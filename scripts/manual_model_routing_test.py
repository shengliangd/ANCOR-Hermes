#!/usr/bin/env python3
"""Exercise the installed Hermes CLI against a local toy API in a disposable home.

Example: python scripts/manual_model_routing_test.py --native /path/to/hermes-agent
Only dependency metadata/libraries are borrowed from the install owning that checkout.
No provider credentials, live config, or live gateway are used.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ancor.store import Store
from ancor.goals import GoalStore
from ancor.watcher import GoalRunner, default_goal_command
from ancor.model_routing import CategoryRouter


def borrow_dependencies(root: Path, native: Path, runtime_home: Path) -> None:
    key = hashlib.sha256(str(native.resolve()).encode()).hexdigest()[:16]
    facts = json.loads((runtime_home / 'installs' / key / 'facts.json').read_text())
    source = Path(facts['packages']['venv']['environment'])
    private = root / 'installs' / key / 'environments' / 'toy' / 'venv'
    private.mkdir(parents=True)
    shutil.copy2(source / 'pyvenv.cfg', private / 'pyvenv.cfg')
    site = next((source / 'lib').glob('python*/site-packages'))
    dest = private / 'lib' / site.parent.name / 'site-packages'
    dest.mkdir(parents=True)
    (dest / 'borrowed-dependencies.pth').write_text(str(site) + '\n')
    (private / 'bin').mkdir()
    for name in ('python', 'python3', site.parent.name):
        (private / 'bin' / name).symlink_to((source / 'bin' / 'python').resolve())
    facts['packages']['venv']['environment'] = str(private)
    facts['packages']['venv'].pop('resolved_lock', None)
    (root / 'installs' / key / 'facts.json').write_text(json.dumps(facts))


def stop_private_gateway(root: Path) -> None:
    pidfile = root / 'gateway.pid'
    if not pidfile.exists():
        return
    info = json.loads(pidfile.read_text())
    pid = info['pid']
    # Verify ownership before signalling; never run a global gateway stop command.
    try:
        process_env = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        if f'HERMES_HOME={root}'.encode() not in process_env:
            raise RuntimeError('Refusing to stop a gateway outside the toy instance')
        os.kill(pid, signal.SIGTERM)
        # Wait for this exact process to finish before deleting its home.
        for _ in range(100):
            stat = Path(f'/proc/{pid}/stat')
            if not stat.exists() or stat.read_text().split(') ', 1)[1].startswith('Z'):
                return
            threading.Event().wait(0.1)
        raise RuntimeError(f'Private gateway {pid} did not stop')
    except ProcessLookupError:
        pass
    except FileNotFoundError:
        pass


def run_probe(root: Path, native: Path, runtime_home: Path, hermes: str) -> dict:
    borrow_dependencies(root, native, runtime_home)
    requests = []
    histories = []
    slow = threading.Event()
    entered = threading.Event()

    class ToyAPI(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_error(404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            if self.path != '/v1/chat/completions':
                self.send_error(404)
                return
            requests.append({'path': self.path, 'model': body.get('model'),
                             'authorization': self.headers.get('Authorization')})
            histories.append(body.get('messages', []))
            if slow.is_set():
                entered.set()
                threading.Event().wait(3)
            model = body['model']
            self.send_response(200)
            if body.get('stream'):
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for delta, finish in [({'role': 'assistant', 'content': 'ANCOR_LOCAL_ROUTE_OK'}, None),
                                      ({}, 'stop')]:
                    chunk = {'id': 'chatcmpl-toy', 'object': 'chat.completion.chunk',
                             'created': 1, 'model': model,
                             'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            else:
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'id': 'chatcmpl-toy', 'object': 'chat.completion', 'created': 1, 'model': model,
                    'choices': [{'index': 0, 'message': {'role': 'assistant',
                                 'content': 'ANCOR_LOCAL_ROUTE_OK'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
                }).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), ToyAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = root / 'profiles' / 'ancor-exec'
    home.mkdir(parents=True)
    endpoint = f'http://127.0.0.1:{server.server_port}/v1'
    config = {
        '_config_version': 12,
        'model': {'provider': 'custom', 'default': 'wrong-default',
                  'base_url': endpoint + '/wrong', 'context_length': 32768},
        'model_aliases': {'research': {'provider': 'custom', 'model': 'toy-routed-model',
                                     'base_url': endpoint, 'key_env': 'ANCOR_TOY_KEY'}},
        'auth': {'adopt_external_logins': False}, 'platform_toolsets': {'cli': []},
        'agent': {'max_turns': 1}, 'plugins': {'enabled': []},
        'telemetry': {'enabled': False}, 'curator': {'enabled': False},
        'auxiliary': {'background_review': {'enabled': False}}, 'terminal': {'cwd': str(root)},
    }
    (root / 'config.yaml').write_text('{}')
    (home / 'config.yaml').write_text(yaml.safe_dump(config))
    (home / '.env').write_text('ANCOR_TOY_KEY=toy-alias-key\nOPENAI_API_KEY=toy-default-key\n')
    (home / '.no-bundled-skills').touch()
    # Fresh environment: no inherited provider, proxy, remote gateway or ANCOR role.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR') if key in os.environ}
    env.update(HERMES_HOME=str(root), HERMES_DISABLE_LAZY_INSTALLS='1',
               PYTHONDONTWRITEBYTECODE='1', ANCOR_TOY_KEY='toy-alias-key',
               OPENAI_API_KEY='toy-default-key', NO_PROXY='127.0.0.1,localhost')
    try:
        result = subprocess.run(
            [hermes, '-p', 'ancor-exec', '--cli', 'chat', '-m', 'research',
             '-q', 'Reply ANCOR_LOCAL_ROUTE_OK'], cwd=root, env=env,
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (result.stdout + result.stderr)[-6000:]
        assert 'ANCOR_LOCAL_ROUTE_OK' in result.stdout
        store = Store(root / 'requests.db')
        proposal = store.propose_change(None, [{'op': 'create', 'title': 'Session probe',
            'fields': {'outcome': ['Verify persistent conversation']}}], actor='default',
            source={'kind': 'interactor_proposal', 'input': 'session probe'})
        rid = store.commit_change(proposal['id'], 'probe-confirm')['request_id']
        router = CategoryRouter(alias_names=lambda: {'research'})
        goals = GoalStore(store, category_router=router)
        goals.spawn_goal({'goal_id': 'probe', 'request_ref': {'id': rid, 'generation': 1},
            'objective': 'Reply ANCOR_LOCAL_ROUTE_OK', 'done_criteria': 'Continuity verified',
            'scope': {'readable_requests': [rid], 'can_ask_human': False}, 'budget': {},
            'category': 'research'})
        goals.register_resource_usage('probe', handle_id='probe-external-work')
        def command(*args, **kwargs):
            return [hermes, *default_goal_command(*args, **kwargs)[1:]]
        runner = GoalRunner(goals, command_builder=command, env_builder=lambda: env, native_sessions=True)
        assert runner.run_goal_sync('probe') == 0
        sid = goals.get_goal('probe')['session_id']
        assert sid and sid != 'goal-probe', sid
        assert goals.get_goal('probe')['status'] == 'waiting_external'
        reopened = GoalStore(Store(store.path), category_router=router)
        reopened.signal_goal('probe', 'recovery', {'message': 'Continue the same conversation'})
        runner = GoalRunner(reopened, command_builder=command, env_builder=lambda: env, native_sessions=True)
        assert runner.run_goal_sync('probe') == 0
        assert reopened.get_goal('probe')['session_id'] == sid
        assert any(m.get('role') == 'assistant' and 'ANCOR_LOCAL_ROUTE_OK' in str(m.get('content'))
                   for m in histories[-1]), 'resumed request lost previous assistant response'
        slow.set()
        reopened.signal_goal('probe', 'steer', {'message': 'Run another turn for interruption test'})
        worker_errors = []
        def running_goal():
            try:
                runner.run_goal_sync('probe')
            except BaseException as exc:
                worker_errors.append(repr(exc))
        worker = threading.Thread(target=running_goal)
        worker.start()
        assert entered.wait(30), 'native execution did not reach the API'
        private_python = next((root / 'installs').glob('*/environments/toy/venv/bin/python'))
        def native_stop(session_id):
            code = ('import sys; sys.path[:0]=' + repr([str(Path(__file__).resolve().parents[1]), str(native)]) +
                    '; from ancor.session_control import confirm_session_stopped; '
                    'sys.exit(0 if confirm_session_stopped(sys.argv[1]) else 1)')
            stopped = subprocess.run([str(private_python), '-c', code, session_id], env=env,
                                     capture_output=True, text=True, timeout=20)
            assert stopped.returncode == 0, stopped.stderr[-3000:]
            return True
        pause = reopened.pause_goal('probe', session_stopper=native_stop)
        worker.join(10)
        assert not worker.is_alive() and not worker_errors, worker_errors
        assert pause['stop_confirmed'], pause
        assert reopened.get_goal('probe')['status'] == 'paused'
        slow.clear()
        assert requests and all(r == {
            'path': '/v1/chat/completions', 'model': 'toy-routed-model',
            'authorization': 'Bearer toy-alias-key',
        } for r in requests), requests
        return {'scenario': 'native_cli_alias_session_resume_and_gateway_stop', 'status': 'PASS',
                'api_requests': len(requests), 'external_model_calls': 0}
    finally:
        try:
            stop_private_gateway(root)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', type=Path, required=True)
    parser.add_argument('--runtime-home', type=Path)
    parser.add_argument('--hermes', default='hermes')
    args = parser.parse_args()
    # Hermes requires private control-socket directories even under a permissive shell umask.
    previous_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix='ancor-native-routing-') as directory:
            print(json.dumps(run_probe(Path(directory), args.native.resolve(),
                                       (args.runtime_home or args.native.parent).resolve(),
                                       args.hermes), indent=2))
    finally:
        os.umask(previous_umask)


if __name__ == '__main__':
    main()
