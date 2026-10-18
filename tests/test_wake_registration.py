"""Public wake schema, role fences, readiness and native observation parity."""
import asyncio
import json
import sys
import time
from types import SimpleNamespace

import ancor
from ancor.host import Host
from ancor.runtime import Coordinator
from ancor.scheduling import Scheduling
from ancor.store import Store


def registered(tmp_path, monkeypatch):
    tools = {}
    config = {'store_path': str(tmp_path / 'db'), 'board': 'test'}
    ctx = SimpleNamespace(profile_name='ancor-leader',
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **kw: tools.update({kw['name']: kw}),
        register_hook=lambda *args: None)
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: [{'id': 'task', 'status': 'running'}])
    ancor.register(ctx)
    store = Store(config['store_path'])
    req = store.confirm(store.create_draft('wait', 'verified')['id'], 'fixture')
    store.link_task(req['id'], 'task')
    store.ack_events([e['id'] for e in store.pending_events()])
    return ctx, tools, store, req


def test_schema_fences_readiness_and_context_parity(tmp_path, monkeypatch):
    monkeypatch.delenv('ANCOR_COORDINATOR', raising=False)
    ctx, tools, store, req = registered(tmp_path, monkeypatch)
    schema = tools['ancor_wake']['schema']['parameters']
    from pathlib import Path
    assert '  - ancor_wake' in (Path(ancor.__file__).parent / 'plugin.yaml').read_text()
    assert schema['properties']['action']['enum'] == ['schedule', 'list', 'cancel', 'replace']
    assert schema['properties']['due_at']['type'] == 'number'
    assert not schema['additionalProperties']
    call = lambda name, args: json.loads(tools[name]['handler'](args))
    args = dict(action='schedule', requirement_id=req['id'], reason='external release', due_at=time.time() + 1000)
    assert 'coordinator run required' in call('ancor_wake', args)['error']
    assert call('ancor_wake', {'action': 'list'}) == []
    context = call('ancor_get_context', {})
    assert context['progression'][0]['sources'] == [{'kind': 'native_task', 'task_id': 'task', 'status': 'running'}]
    assert context['liveness']['last_started'] is None
    host = Host(ctx, store=store, board=SimpleNamespace())
    host.tasks = [{'id': 'task', 'status': 'running'}]
    assert host.snapshot()['progression'] == context['progression']
    monkeypatch.setenv('ANCOR_COORDINATOR', '1')
    wake = call('ancor_wake', args)
    first = call('ancor_get_context', {})['liveness']
    assert first['last_started'] is not None
    assert call('ancor_get_context', {})['liveness']['generation'] == first['generation'] == 1
    replacement = call('ancor_wake', dict(action='replace', wake_id=wake['id'], reason='human still unavailable', due_at=time.time() + 2000))
    assert replacement['id'] != wake['id']
    assert call('ancor_wake', dict(action='cancel', wake_id=replacement['id']))['state'] == 'cancelled'


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
    sources = restarted.observation(store.snapshot())['progression'][0]['sources']
    assert len(sources) == 2
    assert all(s['kind'] == 'scheduled_wake' for s in sources)
    assert not store.pending_events()
    Scheduling(Store(store.path), lambda: 1000).scan()
    assert len([e for e in store.pending_events() if e['kind'] == 'wake.due']) == 2
