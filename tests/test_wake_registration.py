"""Public wake schema, role fences, readiness and native observation parity."""
import asyncio
import json
import sys
import time
from types import SimpleNamespace

import ancor
from ancor.host import Host
from ancor.notification import NotificationOutbox, SenderError
from ancor.runtime import Coordinator
from ancor.scheduling import Scheduling
from ancor.store import Store


def registered(tmp_path, monkeypatch):
    tools = {}
    config = {'store_path': str(tmp_path / 'db'), 'board': 'test',
              'human_target': 'test:destination', 'transport_profile': 'test'}
    ctx = SimpleNamespace(profile_name='ancor-coordinator',
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **kw: tools.update({kw['name']: kw}),
        register_hook=lambda *args: None)
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: [
        {'id': 'task', 'status': 'running', 'last_heartbeat_at': time.time()}])
    ancor.register(ctx)
    store = Store(config['store_path'])
    req = store.confirm(store.create_draft('wait', 'verified')['id'], 'fixture')
    store.link_task(req['id'], 'task')
    store.ack_events([e['id'] for e in store.pending_events()])
    return ctx, tools, store, req


def test_schema_fences_readiness_and_context_parity(tmp_path, monkeypatch):
    ctx, tools, store, req = registered(tmp_path, monkeypatch)
    schema = tools['ancor_wake']['schema']['parameters']
    from pathlib import Path
    assert '  - ancor_wake' in (Path(ancor.__file__).parent / 'plugin.yaml').read_text()
    assert schema['properties']['action']['enum'] == ['schedule', 'list', 'cancel', 'replace']
    assert schema['properties']['due_at']['type'] == 'number'
    assert not schema['additionalProperties']
    call = lambda name, args: json.loads(tools[name]['handler'](args))
    args = dict(action='schedule', requirement_id=req['id'], reason='external release', due_at=time.time() + 1000)
    assert call('ancor_wake', {'action': 'list'})['items'] == []
    context = call('ancor_get_context', {})
    assert context['progression'][0]['state'] == 'active'
    assert context['progression'][0]['reason'] == 'running_heartbeat'
    assert context['liveness']['last_started'] is not None
    host = Host(ctx, store=store, board=SimpleNamespace())
    heartbeat = context['progression'][0]['tasks'][0]['last_heartbeat_at']
    host.tasks = [{'id': 'task', 'status': 'running', 'last_heartbeat_at': heartbeat}]
    assert host.wake_packet()['progression'] == context['progression']
    assert 'error' not in call('ancor_wake', args)
    wake = call('ancor_wake', dict(action='schedule', requirement_id=req['id'], reason='external release 2', due_at=time.time() + 2000))
    first = call('ancor_get_context', {})['liveness']
    assert first['last_started'] is not None
    assert call('ancor_get_context', {})['liveness']['generation'] == first['generation'] == 1
    replacement = call('ancor_wake', dict(action='replace', wake_id=wake['id'], reason='human still unavailable', due_at=time.time() + 2000))
    assert replacement['id'] != wake['id']
    assert call('ancor_wake', dict(action='cancel', wake_id=replacement['id']))['state'] == 'cancelled'


def test_ack_rejects_triage_and_host_stalled(tmp_path, monkeypatch):
    ctx, tools, store, req = registered(tmp_path, monkeypatch)
    call = lambda name, args: json.loads(tools[name]['handler'](args))
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: [
        {'id': 'task', 'status': 'triage'}])
    Scheduling(store).sync_health(store.snapshot(), [{'id': 'task', 'status': 'triage'}], [])
    stalled = next(e for e in store.pending_events() if e['kind'] == 'host.stalled')
    assert 'host-owned' in call('ancor_get_context', {'ack_event_ids': [stalled['id']]})['error']
    repeated = store.add_event('native.block_loop_detected', {'task_id': 'task'},
                               source_key='native:test:triage')
    assert 'triage' in call('ancor_get_context', {'ack_event_ids': [repeated['id']]})['error']
    Scheduling(store).sync_health(store.snapshot(), [
        {'id': 'task', 'status': 'running', 'last_heartbeat_at': time.time()}], [])
    assert not [e for e in store.pending_events() if e['kind'] == 'host.stalled']


def test_ack_does_not_depend_on_question_delivery(tmp_path, monkeypatch):
    ctx, tools, store, req = registered(tmp_path, monkeypatch)
    call = lambda name, args: json.loads(tools[name]['handler'](args))
    tasks = [{'id': 'task', 'status': 'blocked'}]
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: tasks)
    blocked = store.add_event('native.blocked', {'task_id': 'task'},
                              source_key='native:test:blocked')
    assert 'error' not in call('ancor_get_context', {'ack_event_ids': [blocked['id']]})
    context = call('ancor_get_context', {})
    assert context['progression'][0]['state'] == 'stalled'
    assert context['progression'][0]['reason'] == 'blocked_without_wait'

    box = NotificationOutbox(store.path + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    box.enqueue('label-question', 'question', 'Which label?', actor='ancor-coordinator',
                requirement_id=req['id'], task_id='task')
    assert box.claim_next()['state'] == 'sending'
    assert box.mark_uncertain('label-question', SenderError('timeout'))['state'] == 'uncertain'

    blocked_again = store.add_event('native.blocked', {'task_id': 'task'},
                                    source_key='native:test:blocked-again')
    assert 'error' not in call('ancor_get_context', {'ack_event_ids': [blocked_again['id']]})
    context = call('ancor_get_context', {})
    assert context['progression'][0]['state'] == 'stalled'
    assert context['progression'][0]['reason'] == 'question_delivery_unconfirmed'
    Scheduling(store).sync_health(store.snapshot(), tasks,
                                  context['notifications']['items'])
    assert any(e['kind'] == 'host.stalled' for e in store.pending_events())


def test_ack_allows_blocked_task_from_invalidated_requirement(tmp_path, monkeypatch):
    ctx, tools, store, req = registered(tmp_path, monkeypatch)
    call = lambda name, args: json.loads(tools[name]['handler'](args))
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: [
        {'id': 'task', 'status': 'blocked'}])
    replacement = store.confirm(store.create_draft('replacement', 'verified', req['id'])['id'], 'fixture')
    assert replacement['status'] == 'active'
    assert store.get_requirement(req['id'])['status'] == 'invalidated'
    blocked = store.add_event('native.blocked', {'task_id': 'task'},
                              source_key='native:test:obsolete')
    assert 'error' not in call('ancor_get_context', {'ack_event_ids': [blocked['id']]})


def test_failed_cli_never_counts_as_coordination(tmp_path):
    store = Store(tmp_path / 'db')
    scheduler = Scheduling(store, lambda: 100)
    runner = Coordinator(tmp_path / 'lock', pending=lambda n: [{'id': 1}], snapshot=lambda: {}, scan=lambda: None,
                         started=scheduler.started, command=lambda _: [sys.executable, '-c', 'raise SystemExit(2)'])
    assert asyncio.run(runner.tick()) == 'failed'
    assert scheduler.observation(store.snapshot())['liveness']['last_started'] is None


def test_liveness_not_hidden_by_backlog(tmp_path, monkeypatch):
    ctx, _, store, _ = registered(tmp_path, monkeypatch)
    host = Host(ctx, store=store, board=SimpleNamespace(), clock=lambda: 10000)
    host.startup = False
    for _ in range(40):
        store.add_event('input', {})
    insurance = store.add_event('host.liveness', {})
    store.add_event('newest', {})
    assert insurance in host.pending(32)


def test_human_and_external_wait_durable_without_task(tmp_path):
    store = Store(tmp_path / 'db')
    req = store.confirm(store.create_draft('await human and external release', 'verified')['id'], 'fixture')
    store.ack_events([e['id'] for e in store.pending_events()])
    scheduler = Scheduling(store, lambda: 100)
    for reason in ['Human reply follow-up if unanswered', 'External release publication check']:
        scheduler.call(dict(action='schedule', requirement_id=req['id'], reason=reason, due_at=1000))
    restarted = Scheduling(Store(store.path), lambda: 999)
    health = restarted.observation(store.snapshot())['progression'][0]
    assert health['state'] == 'stalled' and health['reason'] == 'no_work'
    assert len(health['timers']) == 2
    assert not store.pending_events()
    Scheduling(Store(store.path), lambda: 1000).scan()
    assert len([e for e in store.pending_events() if e['kind'] == 'wake.due']) == 2
