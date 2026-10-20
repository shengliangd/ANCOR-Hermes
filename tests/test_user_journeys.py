"""Deterministic journeys over native tools and a private real Kanban board.

No provider or messaging E2E claim: only model decisions, clarify UI responses,
and transport are deterministic substitutes. Every task transition uses the
native CLI; no SQL task writes, forced completion, or delegation-fence removal.
Run the full suite from an unfenced parent process. Delegated children can run
confirmation coverage but skip board mutations instead of evading native fences.
"""
import asyncio
import json
import os
from pathlib import Path
import site
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = Path(os.environ.get('ANCOR_NATIVE_HERMES', '/home/user/.hermes/hermes-agent'))
NATIVE_PYTHON = Path(os.environ.get('ANCOR_NATIVE_PYTHON', str(NATIVE / '.venv/bin/python')))
NATIVE_COMMAND_TIMEOUT = 15
_REAL_SUBPROCESS_RUN = subprocess.run


def bounded_native_run(command, **kwargs):
    """Bound real CLI I/O, never substitute a lifecycle result."""
    kwargs['timeout'] = min(kwargs.get('timeout', NATIVE_COMMAND_TIMEOUT), NATIVE_COMMAND_TIMEOUT)
    print('NATIVE COMMAND: ' + repr(command), flush=True)
    if command[0] == 'hermes':
        # Use the real Kanban parser/command in the private root. Full launcher
        # provisioning/profile bootstrap is a separate deployment acceptance gate.
        command = [sys.executable, '-c',
                   'import argparse,sys; from hermes_cli.kanban_parser import build_parser; '
                   'from hermes_cli.kanban import kanban_command; '
                   'p=argparse.ArgumentParser(); build_parser(p.add_subparsers()); '
                   'sys.exit(kanban_command(p.parse_args(sys.argv[1:])))', *command[3:]]
    try:
        return _REAL_SUBPROCESS_RUN(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f'Native command exceeded {NATIVE_COMMAND_TIMEOUT}s: {command!r}\n'
            f'stdout={exc.stdout!r}\nstderr={exc.stderr!r}'
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            f'Native command failed ({exc.returncode}): {command!r}\n'
            f'stdout={exc.stdout}\nstderr={exc.stderr}'
        ) from exc


def fenced():
    return any(os.environ.get(k) for k in (
        'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID'))


@pytest.mark.parametrize('journey', ['confirmation', 'rolling', 'blocker', 'replacement', 'acceptance', 'restart'])
def test_user_journey(tmp_path, journey):
    if not NATIVE.is_dir():
        pytest.skip('installed native Hermes source unavailable')
    if journey != 'confirmation' and fenced():
        pytest.skip('real Kanban mutation requires unfenced parent execution')
    home = tmp_path / 'home'
    home.mkdir()
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(home / '.hermes'),
               HERMES_KANBAN_HOME=str(home / 'board-root'))
    env['PYTHONPATH'] = os.pathsep.join([str(ROOT), str(NATIVE), site.getusersitepackages()])
    result = subprocess.run([str(NATIVE_PYTHON), str(Path(__file__).resolve()),
                             journey], cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    assert 'JOURNEY PASSED: ' + journey in result.stdout


class Journey:
    def __init__(self):
        # Wrap process I/O before NativeBoard captures subprocess.run as its
        # constructor default, including boards lazily created by registered tools.
        # Native command arguments, ownership guards and outcomes remain genuine.
        subprocess.run = bounded_native_run
        from hermes_cli.plugins import PluginContext, get_plugin_manager
        from hermes_cli.plugins_manifest import PluginManifest
        from tools.registry import registry
        from ancor.store import Store
        from ancor.board import NativeBoard
        import ancor
        import tools.clarify_tool  # register the genuine native clarify handler
        self.config = {'store_path': str(Path(os.environ['HOME']) / 'requirements.db'),
                       'board': 'default', 'human_target': 'feishu:simulated-human',
                       'transport_profile': 'default'}
        config = self.config
        class Context(PluginContext):
            role = 'ancor-interactor'
            @property
            def profile_name(self):
                return self.role
            def get_config(self, key, default=None):
                return config.get(key, default)
        self.ctx = Context(PluginManifest(name='ancor'), get_plugin_manager())
        self.coord = Context(PluginManifest(name='ancor-coordinator'), get_plugin_manager())
        self.coord.role = 'ancor-coordinator'
        self.coord.profile_name_override = 'ancor-coordinator'
        self.sent = []
        def send(target, message):
            self.sent.append((target, message))
            return {'success': True, 'message_id': 'simulated-transport-' + str(len(self.sent))}
        self.send = send
        ancor.register(self.ctx)
        ancor.register(self.coord)
        assert registry.get_entry('ancor_requirement', scope=get_plugin_manager().scope_key)
        self.store = Store(config['store_path'])
        # An isolated native profile is needed by NativeBoard's explicit -p.
        profile = Path(os.environ['HERMES_HOME']) / 'profiles' / 'ancor-interactor'
        profile.mkdir(parents=True, exist_ok=True)
        # Supported native opt-outs for automatic model-backed work. There is
        # no goal_judge.enabled switch in this native version: goal judging is
        # controlled by the card's goal_mode (asserted below), not a fake verdict.
        disposable_config = ('kanban:\n  auto_decompose: false\n'
                             'auxiliary:\n  background_review:\n    enabled: false\n'
                             '  title_generation:\n    enabled: false\n')
        for config_home in (Path(os.environ['HERMES_HOME']), profile):
            (config_home / 'config.yaml').write_text(disposable_config, encoding='utf-8')
        self.board = NativeBoard('default')
        self.calls = 0

    def deliver_notifications(self):
        from ancor.notification import NotificationDispatcher, NotificationOutbox
        box = NotificationOutbox(self.store.path + '.notifications.db',
                                 coordinator_profile='ancor-coordinator',
                                 target=self.config['human_target'])
        tasks = {task['id']: task for task in self.board.list()}
        delivery = NotificationDispatcher(
            box, self.send, resolve_requirement=self.store.get_requirement,
            resolve_task=lambda task_id: tasks.get(task_id), retry_seconds=1)
        for _ in range(10):
            if not asyncio.run(delivery.deliver_once()):
                return
        raise AssertionError('notification delivery did not quiesce')

    def call(self, tool, **args):
        context = self.coord if tool in {'board', 'acceptance', 'wake'} else self.ctx
        if tool == 'notify' and args.get('action') == 'respond': context = self.ctx
        value = json.loads(context.dispatch_tool('ancor_' + tool, args))
        assert not value.get('error'), (tool, args, value)
        return value

    def error(self, tool, **args):
        context = self.coord if tool in {'board', 'acceptance', 'wake'} else self.ctx
        value = json.loads(context.dispatch_tool('ancor_' + tool, args))
        assert 'error' in value, (tool, args, value)
        return value['error']

    def draft(self, brief='Deliver report', criteria='Report has exact requested values', replacement_of=None):
        args = dict(action='draft', summary='Deliver the requested result',
                    brief=brief, acceptance_criteria=criteria)
        if replacement_of:
            args['replacement_of'] = replacement_of
        return self.call('requirement', **args)

    def confirm(self, draft, outcome='submitted', choice=1):
        from agent.agent_runtime_helpers import invoke_tool
        from types import SimpleNamespace
        self.calls += 1
        shown = []
        def surface(questions):
            shown.extend(questions)
            return {'outcome': outcome, 'answers': {'q0': questions[0]['choices_offered'][choice]}}
        agent = SimpleNamespace(session_id='journey-session', _memory_manager=None,
                                clarify_callback=surface)
        result = json.loads(invoke_tool(agent, 'clarify', draft['clarify_args'], '',
                                       tool_call_id='journey-call-' + str(self.calls)))
        assert shown and draft['brief'] in shown[0]['question']
        assert result.get('outcome') == outcome, result
        requirements = [r for r in self.store.snapshot()['requirements'] if r['draft_id'] == draft['id']]
        if outcome == 'submitted' and choice == 1:
            assert len(requirements) == 1
            return requirements[0]
        assert not requirements

    def requirement(self, **kwargs):
        return self.confirm(self.draft(**kwargs))

    def create(self, req, key):
        detail = self.call('board', action='create', requirement_id=req['id'], key=key,
                           title='Execute ' + key, body='Produce and verify ' + key,
                           contract=self.contract(key))
        assert detail['task']['assignee'] == 'ancor-worker'
        assert not detail['task'].get('goal_mode', False), 'offline journey must not invoke goal_judge'
        return detail['task']['id']

    @staticmethod
    def contract(key):
        return dict(objective='Execute ' + key, scope='Only ' + key,
                    inputs=['confirmed requirement'], dependencies=[], artifacts=[key + ' artifact'],
                    verification=['Verify ' + key],
                    constraints=['Preserve exact values'],
                    stop_conditions=['Required input or decision is missing'])

    def status(self, tid):
        return self.board.show(tid)['status']

    def ready(self, tid):
        # Explicit native handoff creates a claimable task; dependency blocks
        # without open parents are deliberately sticky in current Hermes.
        if self.status(tid) == 'running':
            self.board._call('block', tid, 'Hand off to deterministic executor', '--kind', 'capability', json_output=False)
            self.board._call('unblock', tid, '--reason', 'Deterministic executor available', json_output=False)
        assert self.status(tid) == 'ready'

    def claim(self, tid):
        self.ready(tid)
        self.board._call('claim', tid, json_output=False)
        detail = self.board._call('show', tid)
        runs = [r for r in detail['runs'] if not r.get('ended_at')]
        assert detail['task']['status'] == 'running' and len(runs) == 1
        return runs[0]['id']

    def worker(self, tid, action, *args):
        from ancor.board import NativeBoard
        runs = [r for r in self.board._call('show', tid)['runs'] if not r.get('ended_at')]
        assert len(runs) == 1, runs
        env = dict(os.environ, HERMES_KANBAN_TASK=tid,
                   HERMES_KANBAN_RUN_ID=str(runs[0]['id']))
        NativeBoard('default', env=env)._call(action, tid, *args, json_output=False)

    def complete(self, tid):
        self.claim(tid)
        artifact = Path(os.environ['HOME']) / (tid + '.txt')
        artifact.write_text('Quarterly report\nTotal: 42\n', encoding='utf-8')
        assert artifact.read_text(encoding='utf-8') == 'Quarterly report\nTotal: 42\n'
        self.worker(tid, 'complete', '--result', 'Deterministic executor produced verified artifact: ' + str(artifact),
                    '--summary', 'Artifact verified by deterministic fixture')
        assert self.status(tid) == 'done'
        runs = self.board._call('show', tid)['runs']
        assert runs and runs[-1]['ended_at']

    def host(self):
        from ancor.host import Host
        from hermes_cli.kanban_db import kanban_db_path
        from ancor.store import Store
        return Host(self.ctx, store=Store(self.store.path), board=self.board,
                    db_path=kanban_db_path(board='default'))

    def evidence(self, req, tids):
        return {'criteria': req['acceptance_criteria'], 'verification': ['Independent coordinator fixture checked exact values'],
                'artifacts': [str(Path(os.environ['HOME']) / (tid + '.txt')) for tid in tids], 'task_ids': tids}


def journey_confirmation(j):
    draft = j.draft()
    j.confirm(draft, outcome='cancelled')
    assert not j.store.pending_events()
    j.confirm(draft, choice=0)
    req = j.confirm(draft)
    j.confirm(draft)
    assert req['status'] == 'active'
    assert len(j.store.snapshot()['requirements']) == 1
    assert len(j.store.pending_events()) == 1


def journey_rolling(j):
    req = j.requirement()
    a = j.create(req, 'A')
    detail = j.call('board', action='create', requirement_id=req['id'], key='B',
                    title='Execute B', body='Depends on A', parent_ids=[a], hold=True,
                    contract=j.contract('B'))
    b = detail['task']['id']
    assert a in detail['parents'] and j.status(b) == 'blocked'
    metadata = j.board.show(b)['body'].split('\n', 1)[0]
    edited = j.call('board', action='edit', task_id=b, title='Execute revised B',
                    body='Produce and verify revised B', priority=7,
                    contract=j.contract('B'))
    assert edited['task']['title'] == 'Execute revised B'
    assert edited['task']['priority'] == 7
    assert edited['task']['body'].split('\n', 1)[0] == metadata
    assert req['acceptance_criteria'] in edited['task']['body']
    linked = j.call('board', action='link', parent_id=a, child_id=b)
    assert a in linked['parents']
    assert j.status(b) == 'blocked'
    unlinked = j.call('board', action='unlink', parent_id=a, child_id=b)
    assert a not in unlinked['parents']
    assert j.status(b) == 'blocked', 'held task cannot escape during dependency edits'
    j.call('board', action='link', parent_id=a, child_id=b)
    j.call('board', action='unblock', task_id=b, reason='Coordinator finished dependency planning')
    assert j.status(b) == 'todo'
    j.complete(a)
    assert j.status(b) == 'ready', 'native dependency completion must automatically release B'
    j.complete(b)
    c = j.create(req, 'C-after-evidence')
    j.complete(c)
    assert {x['task_id'] for x in j.store.snapshot()['task_links']} == {a, b, c}


def journey_blocker(j):
    req = j.requirement()
    tid = j.create(req, 'blocked-worker')
    j.claim(tid)
    j.worker(tid, 'block', 'Need coordinator to identify report source', '--kind', 'needs_input')
    host = j.host()
    host.scan()
    assert any(e['kind'] == 'native.blocked' for e in j.store.pending_events())
    assert not j.sent, 'worker blocker must not automatically notify the human'
    j.call('board', action='comment', task_id=tid, body='Coordinator resolved source: use approved fixture')
    j.call('notify', action='send', id='label-question', kind='question', message='Use label Quarterly?',
           requirement_id=req['id'], task_id=tid)
    j.deliver_notifications()
    answer = 'Yes, use Quarterly. Do not deploy.'
    from ancor.notification import NotificationOutbox
    response = NotificationOutbox(j.store.path + '.notifications.db', coordinator_profile='ancor-coordinator',
                                  target='feishu:simulated-human').respond(
                                      'label-question', answer, 'interactor_transcription:label-question')
    assert response['answer'] == answer
    event = j.store.add_event('notification.response', {
        'notification_id': response['id'], 'body': answer,
        'reference': response['answer_reference'],
        'source': 'interactor_transcription', 'authorization': False},
        source_key='notification.response:' + response['id'])
    assert event['payload']['authorization'] is False
    j.call('board', action='comment', task_id=tid, body=answer)
    j.call('board', action='unblock', task_id=tid, reason='Source and label resolved')
    j.claim(tid)
    j.worker(tid, 'block', 'Worker hit another blocker after resuming', '--kind', 'needs_input')
    assert j.status(tid) == 'triage', 'native repeat-block protection must remain intact'
    j.call('board', action='revise', task_id=tid, reason='Coordinator reviewed repeated blocker; human supplied label')
    j.complete(tid)
    assert len(j.sent) == 1


def journey_replacement(j):
    old = j.requirement(brief='Old report')
    unrelated = j.requirement(brief='Unrelated report')
    old_task = j.create(old, 'old')
    j.claim(old_task)
    other_task = j.create(unrelated, 'other')
    draft = j.draft(brief='Replacement report', replacement_of=old['id'])
    j.confirm(draft, outcome='cancelled')
    assert j.store.get_requirement(old['id'])['status'] == 'active'
    new = j.confirm(draft)
    assert j.store.get_requirement(old['id'])['status'] == 'invalidated'
    assert j.status(old_task) == 'running', 'confirmation records invalidation, not automatic stop'
    inactive_error = j.error('board', action='create', requirement_id=old['id'], key='forbidden',
                             title='No', body='No', contract=j.contract('forbidden'))
    assert 'not active' in inactive_error, inactive_error
    j.complete(other_task)
    j.call('board', action='block', task_id=old_task, reason='Superseded; coordinator stop decision')
    new_task = j.create(new, 'replacement')
    j.complete(new_task)
    assert j.status(other_task) == 'done'
    assert j.store.get_requirement(unrelated['id'])['status'] == 'active'


def journey_acceptance(j):
    req = j.requirement()
    tid = j.create(req, 'initial')
    abandoned = j.create(req, 'abandoned-alternative')
    j.call('board', action='block', task_id=abandoned, reason='Coordinator chooses replacement approach')
    j.call('board', action='retire', requirement_id=req['id'], task_id=abandoned,
           reason='Alternative abandoned; preserve history, replacement covers criteria')
    premature = dict(action='send', id='accept:' + req['id'], requirement_id=req['id'],
                     kind='completion', message='Report delivered')
    assert 'accepted' in j.error('notify', **premature)
    j.complete(tid)
    j.call('acceptance', action='reject', requirement_id=req['id'], reason='Independent check found missing total')
    assert j.store.get_requirement(req['id'])['status'] == 'active'
    repair = j.create(req, 'repair-total')
    evidence = j.evidence(req, [tid, repair])
    assert 'all linked native tasks' in j.error('acceptance', action='accept', requirement_id=req['id'], evidence=evidence)
    j.complete(repair)
    assert j.call('acceptance', action='accept', requirement_id=req['id'], evidence=evidence)['status'] == 'accepted'
    j.call('notify', **premature)
    j.call('notify', **premature)
    j.deliver_notifications()
    assert len(j.sent) == 1
    assert j.call('notify', action='list')['notifications'][0]['state'] == 'sent'


def journey_restart(j):
    req = j.requirement()
    tid = j.create(req, 'stable-key')
    assert j.create(req, 'stable-key') == tid
    j.complete(tid)
    host = j.host()
    host.scan()
    before = j.store.pending_events()
    assert any(e['kind'] == 'native.completed' for e in before)
    # Crash after Store ingestion but before host cursor persistence: remove
    # only the simulation-owned cursor file, then replay actual native history.
    host.state_path.unlink()
    restarted = j.host()
    restarted.scan()
    assert j.store.pending_events() == before
    ids = [e['id'] for e in before if e['kind'] != 'host.stalled']
    j.call('get_context', ack_event_ids=ids)
    j.host().scan()
    assert not [e for e in j.store.pending_events() if e['kind'] != 'host.stalled']
    assert 'host-owned' in j.error('get_context', ack_event_ids=[
        e['id'] for e in before if e['kind'] == 'host.stalled'])
    assert len(j.store.snapshot()['task_links']) == 1


if __name__ == '__main__':
    name = sys.argv[1]
    if name != 'confirmation' and fenced():
        raise SystemExit('Refusing native board mutation from delegated/worker context; run from parent')
    globals()['journey_' + name](Journey())
    print('JOURNEY PASSED: ' + name)
