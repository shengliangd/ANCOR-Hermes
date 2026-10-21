import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from ancor.board import NativeBoard
from ancor.host import Host, child_environment, coordinator_command, scan_runner
from ancor.store import Store


class Context:
    profile_name = 'default'

    def __init__(self, path):
        self.config = {'store_path': str(path), 'gateway_owner': True, 'board': 'test-board'}

    def get_config(self, key, default=None):
        return self.config.get(key, default)


class Board:
    link = staticmethod(NativeBoard.link)

    def __init__(self, task=None):
        self.tasks = [task] if task else []
        self.history = []
        self.guards = []

    def guard(self, active):
        self.guards.append(active)

    def list(self):
        return self.tasks

    def events(self, db_path, task_ids, *, after=0, limit=100):
        return [e for e in self.history if e['id'] > after and e['task_id'] in task_ids][:limit]


def pending_native(store):
    return [e for e in store.pending_events() if e['kind'].startswith('native.')]


@pytest.mark.parametrize('kind', [
    'completed', 'blocked', 'review_requested', 'dependency_wait',
    'gave_up', 'timed_out', 'spawn_failed', 'crashed', 'stale',
    'rate_limited', 'block_loop_detected', 'protocol_violation',
    'completion_blocked_hallucination', 'completion_blocked_empty_result',
    'suspected_hallucinated_references', 'changes_requested',
    'descendant_invalidated',
])
def test_native_signal_reaches_coordinator_and_recovers_link(tmp_path, monkeypatch, kind):
    ctx = Context(tmp_path / 'store.db')
    store = Store(ctx.config['store_path'])
    req = store.confirm(store.create_draft('deliver', 'verified')['id'], 'human:1')
    store.ack_events([e['id'] for e in store.pending_events()])
    metadata = {'requirement_id': req['id'], 'revision': 1, 'key': 'worker'}
    board = Board({'id': 't1', 'title': 'execution', 'body': 'ANCOR-LINK:' + json.dumps(metadata), 'status': 'done'})
    board.history = [{'id': 17, 'task_id': 't1', 'kind': kind, 'payload': {'reason': 'attention'}}]
    host = Host(ctx, store=store, board=board, db_path=tmp_path / 'native.db')
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        assert kwargs['stdin'] == asyncio.subprocess.DEVNULL
        class Process:
            returncode = 0
            async def wait(self):
                return 0
        return Process()

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    assert asyncio.run(host.coordinator.tick()) == 'ran'
    payload = json.loads(calls[0][-1].split('\n', 1)[1])
    event = next(e for e in payload['events'] if e['kind'] == 'native.' + kind)
    assert event['source_key'] == 'native:test-board:17'
    assert 'attention' in event['payload_preview']
    assert event['task_id'] == 't1'
    assert payload['snapshot']['board']['items'] == []
    assert payload['snapshot']['board_counts']['by_status']['done'] == 1
    assert store.snapshot()['task_links'] == [{'requirement_id': req['id'], 'task_id': 't1'}]
    assert store.pending_events()  # successful exit is NOT an ack
    restarted = Host(ctx, store=store, board=board, db_path=tmp_path / 'native.db')
    restarted.scan()
    assert len(pending_native(store)) == 1
    assert restarted.state()['task_cursors'] == {'t1': 17}
    # Replay the durable inbox write after a crash before cursor persistence.
    restarted.save({'task_cursors': {}})
    restarted.scan()
    assert len(pending_native(store)) == 1
    store.ack_events([e['id'] for e in store.pending_events()])
    restarted.save({'task_cursors': {}})
    restarted.scan()
    assert not pending_native(store)  # replay must not resurrect handled work
    assert board.guards == []


def test_invalidated_requirements_wake_coordinator_without_automatic_guard(tmp_path, monkeypatch):
    ctx = Context(tmp_path / 'store.db')
    store = Store(ctx.config['store_path'])
    old = store.confirm(store.create_draft('old', 'old')['id'], 'human:old')
    store.ack_events([e['id'] for e in store.pending_events()])
    new = store.confirm(store.create_draft('new', 'new', old['id'])['id'], 'human:new')
    metadata = {'requirement_id': old['id'], 'revision': 1, 'key': 'worker'}
    board = Board({'id': 'old-worker', 'status': 'running',
                   'body': 'ANCOR-LINK:' + json.dumps(metadata)})
    # A fresh Store/Host must recover the durable event, not depend on memory.
    host = Host(ctx, board=board, store=Store(ctx.config['store_path']),
                db_path=tmp_path / 'native.db')
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        class Process:
            returncode = 0
            async def wait(self):
                return 0
        return Process()

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    assert asyncio.run(host.coordinator.tick()) == 'ran'
    payload = json.loads(calls[0][-1].split('\n', 1)[1])
    invalidated = [e for e in payload['events'] if e['kind'] == 'requirement.invalidated']
    assert len(invalidated) == 1
    assert invalidated[0]['requirement_id'] == old['id']
    assert new['id'] in invalidated[0]['payload_preview']
    assert board.guards == []
    assert board.tasks[0]['status'] == 'running'
    assert payload['snapshot']['invalidated_tasks']['items'][0]['task_status'] == 'running'
    assert payload['snapshot']['board']['items'] == []
    assert [r['id'] for r in host.wake_packet()['requirements']['items']] == [new['id']]
    assert invalidated[0]['id'] in [e['id'] for e in store.pending_events()]


def test_retry_budget_durable_bounded_and_recovers(tmp_path):
    ctx = Context(tmp_path / 'store.db')
    now = [0]
    host = Host(ctx, board=Board(), clock=lambda: now[0])
    for i in range(40):
        host.store.add_event('signal', {'n': i})
    for _ in range(3):
        assert len(host.pending(32)) == 32
    host = Host(ctx, board=Board(), clock=lambda: now[0])
    assert host.pending(32) == []
    assert host.pending(32) == []
    now[0] = 3600
    assert len(host.pending(32)) == 32
    assert len(host.store.pending_events()) == 40


def test_child_routing_removed_but_native_fences_preserved(monkeypatch):
    monkeypatch.setenv('HERMES_SESSION_ID', 'parent')
    monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', 'guard')
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', 'run')
    env = child_environment()
    assert 'HERMES_SESSION_ID' not in env
    assert env['HERMES_DELEGATED_CHILD_CONTEXT'] == 'guard'
    assert env['HERMES_KANBAN_RUN_ID'] == 'run'
    cmd = coordinator_command('named-coordinator', 'prompt')
    assert ['hermes', '-p', 'named-coordinator', '--cli', 'chat', '-q', 'prompt'] == cmd[-7:]
    assert 'HERMES_DELEGATED_CHILD_CONTEXT' not in cmd


def test_lifecycle_waits_for_native_connected_property(tmp_path, monkeypatch):
    import ancor.host as module
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    adapter = SimpleNamespace(is_connected=False)
    ticks = []
    sleeps = []
    async def tick():
        ticks.append(True)
        adapter.is_connected = False
        return 'idle'
    def factory(ctx):
        assert adapter.is_connected
        def notification_dispatcher():
            raise ValueError('not configured in lifecycle test')
        return SimpleNamespace(coordinator=SimpleNamespace(tick=tick),
                               notification_dispatcher=notification_dispatcher)
    async def sleep(delay):
        sleeps.append(delay)
        if not ticks:
            adapter.is_connected = True
    monkeypatch.setattr(module, 'Host', factory)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    asyncio.run(scan_runner(Context(tmp_path / 'store.db'), adapter))
    assert ticks == [True]
    assert sleeps == [15, 15]


def test_host_closes_questions_for_terminal_requirement(tmp_path):
    from ancor.notification import NotificationOutbox
    ctx = Context(tmp_path / 'store.db')
    store = Store(ctx.config['store_path'])
    req = store.confirm(store.create_draft('deliver', 'verified')['id'], 'human')
    store.link_task(req['id'], 'task')
    box = NotificationOutbox(store.path + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    box.enqueue('q1', 'question', 'Which value?', actor='ancor-coordinator',
                requirement_id=req['id'], task_id='task')
    store.cancel([req['id']], 'user withdrew it')
    host = Host(ctx, store=store, board=Board({'id': 'task', 'status': 'blocked'}),
                db_path=tmp_path / 'native.db')
    host.scan()
    row = box.get('q1')
    assert row['closed'] is True
    assert row['close_reason'] == 'requirement_cancelled'
    assert row['answer'] is None


def test_host_closes_question_when_task_is_no_longer_blocked(tmp_path):
    from ancor.notification import NotificationOutbox
    ctx = Context(tmp_path / 'store.db')
    ctx.config['human_target'] = 'test:destination'
    store = Store(ctx.config['store_path'])
    req = store.confirm(store.create_draft('deliver', 'verified')['id'], 'human')
    store.link_task(req['id'], 'task')
    task = {'id': 'task', 'status': 'blocked'}
    box = NotificationOutbox(store.path + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    box.enqueue('q1', 'question', 'Which value?', actor='ancor-coordinator',
                requirement_id=req['id'], task_id='task')
    host = Host(ctx, store=store, board=Board(task), db_path=tmp_path / 'native.db')
    host.scan()
    assert box.get('q1')['closed'] is False
    task['status'] = 'running'
    host.scan()
    row = box.get('q1')
    assert row['closed'] is True
    assert row['close_reason'] == 'task_not_blocked'


def test_native_event_api_to_store_and_prompt(tmp_path, monkeypatch):
    import sqlite3
    from pathlib import Path
    source = Path('/home/user/.hermes/hermes-agent')
    if not source.exists():
        pytest.skip('installed Hermes source unavailable')
    monkeypatch.syspath_prepend(str(source))
    db = tmp_path / 'native.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER, run_id INTEGER)')
        conn.execute("INSERT INTO task_events VALUES (9, 'worker', 'blocked', '{}', 1, NULL)")
    ctx = Context(tmp_path / 'store.db')
    store = Store(ctx.config['store_path'])
    req = store.confirm(store.create_draft('deliver', 'checked')['id'], 'human:1')
    store.link_task(req['id'], 'worker')
    store.ack_events([e['id'] for e in store.pending_events()])
    board = Board()
    board.events = NativeBoard.events
    host = Host(ctx, store=store, board=board, db_path=db)
    host.scan()
    assert host.pending(32)[0]['kind'] == 'native.blocked'
    assert host.pending(32)[0]['payload']['task_id'] == 'worker'
    assert host.state()['task_cursors'] == {'worker': 9}


def test_idle_never_spawns(tmp_path, monkeypatch):
    host = Host(Context(tmp_path / 'store.db'), board=Board())
    async def forbidden(*args, **kwargs):
        raise AssertionError('idle spawned a model')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    assert asyncio.run(host.coordinator.tick()) == 'idle'


def test_paused_requirement_remains_visible_without_liveness(tmp_path):
    ctx = Context(tmp_path / 'store.db')
    store = Store(ctx.config['store_path'])
    req = store.confirm(store.create_draft('pause me', 'verified')['id'], 'human:1')
    store.link_task(req['id'], 'task')
    pause = store.create_requirement_command('pause', req['id'], 'user asked', 'Stop briefly')
    store.confirm_requirement_command(pause['id'], 'pause-confirmation')
    host = Host(ctx, store=store, board=Board({'id': 'task', 'status': 'blocked'}))
    snapshot = host.wake_packet()
    assert [item['id'] for item in snapshot['requirements']['items']] == [req['id']]
    assert snapshot['requirements']['items'][0]['status'] == 'paused'
    assert snapshot['progression'][0]['state'] == 'paused'
    assert snapshot['liveness']['due_at'] is None


def test_stalled_condition_backs_off_for_one_hour_and_rearms_on_reason_change(tmp_path):
    now = [1000]
    ctx = Context(tmp_path / 'store.db')
    store = Store(ctx.config['store_path'])
    requirement = store.confirm(store.create_draft('wait', 'verified')['id'], 'human:1')
    store.link_task(requirement['id'], 'task')
    store.ack_events([event['id'] for event in store.pending_events()])
    task = {'id': 'task', 'status': 'blocked'}
    host = Host(ctx, store=store, board=Board(task), db_path=tmp_path / 'native.db',
                clock=lambda: now[0])
    host.startup = False
    host.scan()
    key = 'stalled:' + requirement['id']
    condition = host.state()['conditions'][key]
    assert condition == {'reason': 'blocked_without_wait', 'since': 1000,
                         'generation': 1, 'last_woken_at': None}

    assert [event['kind'] for event in host.pending(32)] == ['host.stalled']
    assert host.state()['conditions'][key]['last_woken_at'] == 1000
    assert host.pending(32) == []
    now[0] = 4599
    assert host.pending(32) == []
    now[0] = 4600
    assert [event['kind'] for event in host.pending(32)] == ['host.stalled']
    store.add_event('native.completed', {'task_id': 'task'})
    ordinary = [event['kind'] for event in host.pending(32)]
    assert ordinary == ['native.completed']
    assert 'host.stalled' not in ordinary

    task['status'] = 'triage'
    host.scan()
    condition = host.state()['conditions'][key]
    assert condition['reason'] == 'triage'
    assert condition['generation'] == 2
    assert condition['last_woken_at'] is None
    assert [event['kind'] for event in host.pending(32)] == [
        'native.completed', 'host.stalled']
    packet = host.wake_packet()
    assert packet['conditions']['items'][0]['reason'] == 'triage'
    assert packet['conditions']['items'][0]['generation'] == 2


def test_per_task_cursors_preserve_racing_and_newly_discovered_history(tmp_path):
    ctx = Context(tmp_path / 'store.db')
    store = Store(ctx.config['store_path'])
    req = store.confirm(store.create_draft('deliver', 'checked')['id'], 'human:1')
    for task in ('a', 'b'):
        store.link_task(req['id'], task)
    store.ack_events([e['id'] for e in store.pending_events()])
    board = Board()
    board.history = [{'id': 100, 'task_id': 'a', 'kind': 'completed'}]
    original = board.events
    def events(db_path, task_ids, **kwargs):
        result = []
        for task_id in task_ids:
            result.extend(original(db_path, [task_id], **kwargs))
            if task_id == 'a' and len(board.history) == 1:
                board.history.extend([{'id': 101, 'task_id': 'a', 'kind': 'blocked'},
                                      {'id': 102, 'task_id': 'b', 'kind': 'completed'}])
        return result
    board.events = events
    host = Host(ctx, store=store, board=board, db_path=tmp_path / 'native.db')
    host.scan()
    native_ids = lambda: {e['payload']['id'] for e in pending_native(store)}
    assert native_ids() == {100, 102}
    host.scan()
    assert native_ids() == {100, 101, 102}
    store.link_task(req['id'], 'new')
    board.history.append({'id': 2, 'task_id': 'new', 'kind': 'blocked'})
    Host(ctx, store=store, board=board, db_path=tmp_path / 'native.db').scan()
    assert native_ids() == {2, 100, 101, 102}


def test_bookkeeping_events_do_not_self_wake(tmp_path):
    ctx = Context(tmp_path / 'store.db')
    board = Board()
    board.tasks = [{'id': 'a', 'status': 'running', 'last_heartbeat_at': time.time()}]
    host = Host(ctx, board=board, db_path=tmp_path / 'native.db')
    req = host.store.confirm(host.store.create_draft('x', 'y')['id'], 'human')
    host.store.link_task(req['id'], 'a')
    host.store.ack_events([e['id'] for e in host.store.pending_events()])
    ignored = ['created', 'commented', 'edited', 'attached', 'linked',
               'heartbeat', 'claim_extended', 'claimed', 'spawned',
               'worker_registered', 'respawn_guarded', 'skipped_nonspawnable',
               'reclaim_deferred', 'status', 'assigned', 'promoted', 'unblocked']
    actionable = ['completed', 'blocked', 'review_requested', 'gave_up',
                  'timed_out', 'spawn_failed']
    kinds = ignored + actionable
    host.board.history = [{'id': i, 'task_id': 'a', 'kind': kind}
                          for i, kind in enumerate(kinds, 1)]
    host.scan()
    assert [e['kind'] for e in pending_native(host.store)] == [
        'native.' + k for k in actionable]
    assert host.state()['task_cursors']['a'] == len(kinds)
    host.store.ack_events([e['id'] for e in host.store.pending_events()])
    # Fresh comments/edits and repeated dispatcher deferrals after coordinator
    # handling are also quiet, not merely an already-consumed cursor replay.
    board.history.extend({'id': len(kinds) + i, 'task_id': 'a', 'kind': kind}
                              for i, kind in enumerate(ignored, 1))
    restarted = Host(ctx, board=host.board, db_path=tmp_path / 'native.db')
    # Startup reconciliation is independent of bookkeeping events. Consume its
    # one durable wake without invoking a real model in this unit test.
    recovery = restarted.pending(32)
    assert [e['kind'] for e in recovery] == ['host.recovery']
    host.store.ack_events([e['id'] for e in recovery])
    assert asyncio.run(restarted.coordinator.tick()) == 'idle'
    assert restarted.state()['task_cursors']['a'] == len(kinds) + len(ignored)
    assert not pending_native(host.store)


def test_scan_ingests_despite_individual_read_failure(tmp_path):
    ctx = Context(tmp_path / 'store.db')
    board = Board()
    host = Host(ctx, board=board, db_path=tmp_path / 'native.db')
    req = host.store.confirm(host.store.create_draft('x', 'y')['id'], 'human')
    for task in ('bad', 'good'):
        host.store.link_task(req['id'], task)
    host.store.ack_events([e['id'] for e in host.store.pending_events()])

    original = board.events
    def events(db_path, task_ids, **kwargs):
        if task_ids == ['bad']:
            raise RuntimeError('read failed')
        return original(db_path, task_ids, **kwargs)
    board.events = events
    board.history = [{'id': 8, 'task_id': 'good', 'kind': 'blocked'}]
    host.scan()
    assert [e['payload']['id'] for e in pending_native(host.store)] == [8]
    assert host.state()['task_cursors'] == {'good': 8}
    assert board.guards == []


def test_owner_only(tmp_path):
    ctx = Context(tmp_path / 'store.db')
    ctx.profile_name = 'ancor-coordinator'
    with pytest.raises(PermissionError):
        asyncio.run(scan_runner(ctx, SimpleNamespace(is_connected=True)))
