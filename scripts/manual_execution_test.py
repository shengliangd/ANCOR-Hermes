#!/usr/bin/env python3
"""Run real Root, Goal and Judge tool calls on one private Hermes gateway with a local scripted API."""
from __future__ import annotations
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ancor.goals import GoalStore
from ancor.host import Host, root_command
from ancor.store import Store
from ancor.watcher import GoalRunner
from manual_model_routing_test import borrow_dependencies, stop_private_gateway


def run(root, native):
    borrow_dependencies(root, native, native.parent)
    store = Store(root / 'requests.db')
    proposal = store.propose_change(None, [{'op': 'create', 'title': 'Native execution',
        'fields': {'outcome': ['Verify role-bound execution']}}], actor='default',
        source={'kind': 'interactor_proposal', 'input': 'probe'})
    rid = store.commit_change(proposal['id'], 'probe-confirm')['request_id']
    goals = GoalStore(store)
    goals.spawn_goal({'goal_id': 'native_goal', 'request_ref': {'id': rid, 'generation': 1},
                     'objective': 'Run native goal probe', 'done_criteria': 'Receipt persisted'})
    actions = {}
    tool_results = []
    class API(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_error(404)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            messages = body['messages']
            user = max(i for i, m in enumerate(messages) if m['role'] == 'user')
            text = str(messages[user]['content'])
            role = 'judge' if 'independent ANCOR delivery Judge' in text else ('goal' if 'ANCOR /goal' in text else 'root')
            after = messages[user + 1:]
            results = [m for m in after if m['role'] == 'tool']
            if results:
                tool_results.extend((role, m['content']) for m in results)
                delta, finish = {'role': 'assistant', 'content': 'NATIVE_EXECUTION_OK'}, 'stop'
            else:
                name, args = actions[role]
                available = {t['function']['name'] for t in body.get('tools', [])}
                if 'tool_call' in available:
                    name, args = 'tool_call', {'calls': [{'name': name, 'arguments': args}]}
                delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call-probe', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}]}
                finish = 'tool_calls'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for data, end in [(delta, None), ({}, finish)]:
                chunk = {'id': 'native-probe', 'object': 'chat.completion.chunk', 'created': 1,
                         'model': body['model'], 'choices': [{'index': 0, 'delta': data, 'finish_reason': end}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
    server = ThreadingHTTPServer(('127.0.0.1', 0), API)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = root / 'profiles' / 'ancor-exec'
    profile.mkdir(parents=True)
    settings = {'store_path': store.path, 'owner_profile': 'default', 'gateway_owner': False, 'exec_profile': 'ancor-exec'}
    config = {'_config_version': 12,
        'model': {'provider': 'custom', 'default': 'toy-execution', 'context_length': 131072,
                  'base_url': f'http://127.0.0.1:{server.server_port}/v1'},
        'agent': {'max_turns': 4}, 'auth': {'adopt_external_logins': False},
        'plugins': {'enabled': ['ancor'], 'entries': {'ancor': {'enabled': True, 'settings': settings}}},
        'platform_toolsets': {'cli': ['ancor']}, 'terminal': {'cwd': str(root)},
        'telemetry': {'enabled': False}, 'curator': {'enabled': False},
        'auxiliary': {'background_review': {'enabled': False}}}
    (root / 'config.yaml').write_text(yaml.safe_dump({**config, 'plugins': {'enabled': []}}))
    (profile / 'config.yaml').write_text(yaml.safe_dump(config))
    for home in (root, profile):
        (home / '.env').write_text('OPENAI_API_KEY=toy-key\n')
        (home / '.no-bundled-skills').touch()
    shutil.copytree(Path(__file__).resolve().parents[1] / 'ancor', profile / 'plugins' / 'ancor',
                    ignore=shutil.ignore_patterns('__pycache__'))
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ}
    env.update(HERMES_HOME=str(root), HERMES_DISABLE_LAZY_INSTALLS='1', PYTHONDONTWRITEBYTECODE='1',
               OPENAI_API_KEY='toy-key', NO_PROXY='127.0.0.1,localhost')
    try:
        actions['root'] = ('ancor_goal_list', {})
        result = subprocess.run(root_command('ancor-exec', 'Native root probe'), env=env,
                                cwd=root, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
        assert any(role == 'root' and 'native_goal' in str(value) for role, value in tool_results), tool_results
        actions['goal'] = ('ancor_goal_report', {'request_generation': 1, 'status': 'completed', 'summary': 'Native receipt'})
        runner = GoalRunner(goals, env_builder=lambda: env)
        assert runner.run_goal_sync('native_goal') == 0
        assert goals.get_goal('native_goal')['status'] == 'completed', tool_results
        class Context:
            profile_name = 'default'
            def get_config(self, key, default=None):
                return settings.get(key, default)
        async def launch_judge(packet):
            actions['judge'] = ('ancor_judge_verdict', {'request_id': rid, 'generation': 1,
                'delivery_event_id': packet['delivery_event_id'], 'decision': 'reject',
                'summary': 'Native independent review', 'feedback': ['Additional evidence needed']})
            result = await asyncio.to_thread(subprocess.run, host.judge_command_for(packet), env=env,
                                              cwd=root, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
            return result.returncode
        host = Host(Context(), store=store, judge_launcher=launch_judge)
        packet = host.judge.build_judge_packet(rid, summary='Native delivery', artifacts=[])
        store.add_event('delivery.submitted', {'request_id': rid, 'judge_packet': packet})
        asyncio.run(host.dispatch_judges())
        assert len(host.judge.list_verdicts(rid)) == 1, tool_results
        assert not any(e['kind'] == 'delivery.submitted' for e in store.pending_events()), tool_results
        return {'scenario': 'native_root_goal_judge_same_gateway', 'status': 'PASS',
                'roles_verified': sorted({r for r, _ in tool_results}), 'external_model_calls': 0}
    finally:
        stop_private_gateway(root)
        server.shutdown()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', type=Path, required=True)
    args = parser.parse_args()
    previous = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix='ancor-native-execution-') as path:
            print(json.dumps(run(Path(path), args.native.resolve()), indent=2))
    finally:
        os.umask(previous)


if __name__ == '__main__':
    main()
