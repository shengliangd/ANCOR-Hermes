"""Offline usage simulation: real SQLite, real subprocess/flock, fake wall clock."""
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor

from ancor.store import Store
from ancor.scheduling import Scheduling
from ancor.runtime import Coordinator


def test_usage_simulation(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / 'requirements.db')
    scheduler = Scheduling(store, lambda: now[0])
    req = store.confirm(store.create_draft('long wait', 'verified')['id'], 'test')
    store.ack_events([e['id'] for e in store.pending_events()])
    scheduler.scan()
    assert scheduler.observation(store.snapshot())['progression'][0]['state'] == 'stalled'
    wake = scheduler.call(dict(action='schedule', requirement_id=req['id'], reason='external release tomorrow', due_at=90000))
    assert not store.pending_events()
    stalled = scheduler.observation(store.snapshot())['progression'][0]
    assert stalled['state'] == 'stalled' and stalled['timers']
    now[0] = 8200
    scheduler.scan()
    insurance = store.pending_events()
    assert [e['kind'] for e in insurance] == ['host.liveness']
    # Restarts and concurrent scanners deduplicate the same deadline.
    scheduler = Scheduling(Store(store.path), lambda: now[0])
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: scheduler.scan(), range(8)))
    assert store.pending_events() == insurance
    runner = Coordinator(tmp_path / 'lock', pending=lambda n: store.pending_events()[:n], snapshot=store.snapshot, scan=scheduler.scan, started=scheduler.started, clock=lambda: now[0], command=lambda _: ['/nonexistent/ancor-test'])
    assert asyncio.run(runner.tick()) == 'failed'
    assert scheduler.observation(store.snapshot())['liveness']['last_started'] is None
    now[0] += 10
    runner.command = lambda _: [sys.executable, '-c', 'pass']
    assert asyncio.run(runner.tick()) == 'ran'
    assert scheduler.observation(store.snapshot())['liveness']['last_started'] == now[0]
    assert not store.pending_events()
    # A normal event start resets the deadline as well.
    now[0] += 100
    event = store.add_event('test.input', {})
    assert asyncio.run(runner.tick()) == 'ran'
    store.ack_events([event['id']])
    now[0] += 7199
    scheduler.scan()
    assert not store.pending_events()
    now[0] = 90001
    scheduler.scan()
    assert len([e for e in store.pending_events() if e['kind'] == 'wake.due']) == 1
    replacement = scheduler.call(dict(action='replace', wake_id=wake['id'], reason='release delayed', due_at=100000))
    assert not [e for e in store.pending_events() if e['kind'] == 'wake.due']
    scheduler.call(dict(action='cancel', wake_id=replacement['id']))
    pending = scheduler.call(dict(action='schedule', requirement_id=req['id'], reason='recheck', due_at=100000))
    store.accept(req['id'], 'verified offline')
    scheduler.scan()
    assert next(w for w in scheduler.call({'action': 'list'}) if w['id'] == pending['id'])['state'] == 'suppressed'
    store.ack_events([e['id'] for e in store.pending_events()])
    now[0] = 200000
    scheduler.scan()
    assert not store.pending_events()
    assert scheduler.observation(store.snapshot())['liveness']['due_at'] is None


def test_invalidation_and_validation(tmp_path):
    import pytest
    store = Store(tmp_path / 'db')
    scheduler = Scheduling(store, lambda: 100)
    req = store.confirm(store.create_draft('a', 'b')['id'], 'yes')
    for due in [100, 99, float('nan'), float('inf'), True]:
        with pytest.raises(ValueError):
            scheduler.call(dict(action='schedule', requirement_id=req['id'], reason='x', due_at=due))
    wake = scheduler.call(dict(action='schedule', requirement_id=req['id'], reason='x', due_at=101))
    store.confirm(store.create_draft('new', 'criteria', req['id'])['id'], 'yes')
    scheduler.scan()
    assert scheduler.call({'action': 'list'})[0]['state'] == 'suppressed'


def test_running_coordinator_never_overlaps(tmp_path):
    import fcntl
    store = Store(tmp_path / 'db')
    now = [100]
    scheduler = Scheduling(store, lambda: now[0])
    store.confirm(store.create_draft('a', 'b')['id'], 'yes')
    scheduler.scan()
    scheduler.started()
    now[0] = 7400
    lock = tmp_path / 'lock'
    runner = Coordinator(lock, pending=lambda n: store.pending_events(), snapshot=store.snapshot, scan=scheduler.scan, started=scheduler.started)
    with lock.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert asyncio.run(runner.tick()) == 'busy'
        assert scheduler.observation(store.snapshot())['liveness']['overdue']
    assert scheduler.observation(store.snapshot())['liveness']['last_started'] == 100


def health_fixture(tmp_path):
    store = Store(tmp_path / 'health.db')
    scheduler = Scheduling(store, lambda: 10_000)
    req = store.confirm(store.create_draft('health', 'verified')['id'], 'fixture')
    store.ack_events([e['id'] for e in store.pending_events()])
    return store, scheduler, req


def test_health_distinguishes_triage_blocked_wait_and_stale_heartbeat(tmp_path):
    store, scheduler, req = health_fixture(tmp_path)
    store.link_task(req['id'], 'task')
    data = store.snapshot()
    question = {'id': 'q1', 'kind': 'question', 'requirement_id': req['id'],
                'task_id': 'task', 'state': 'sent', 'answer': None}
    assert scheduler.health(data, [{'id': 'task', 'status': 'triage'}], [question])[0]['state'] == 'stalled'
    assert scheduler.health(data, [{'id': 'task', 'status': 'blocked'}], [question])[0]['state'] == 'waiting_on_human'
    blocked = scheduler.health(data, [{'id': 'task', 'status': 'blocked'}], [])[0]
    assert blocked['state'] == 'stalled' and blocked['reason'] == 'blocked_without_wait'
    assert scheduler.health(data, [{'id': 'task', 'status': 'running', 'last_heartbeat_at': 9_999}], [])[0]['state'] == 'active'
    assert scheduler.health(data, [{'id': 'task', 'status': 'running', 'last_heartbeat_at': 1}], [])[0]['reason'] == 'stale_heartbeat'
    assert scheduler.health(data, [{'id': 'task', 'status': 'ready'}], [])[0]['state'] == 'active'


def test_answered_uncertain_question_is_progress_not_delivery_failure(tmp_path):
    store, scheduler, req = health_fixture(tmp_path)
    store.link_task(req['id'], 'task')
    question = {'id': 'q1', 'kind': 'question', 'requirement_id': req['id'],
                'task_id': 'task', 'state': 'uncertain', 'answer': 'Use quarterly'}
    result = scheduler.health(store.snapshot(), [{'id': 'task', 'status': 'blocked'}], [question])[0]
    assert result['state'] == 'stalled'
    assert result['reason'] == 'question_answered_not_resumed'


def test_wake_is_a_timer_not_a_progress_source(tmp_path):
    store, scheduler, req = health_fixture(tmp_path)
    scheduler.call(dict(action='schedule', requirement_id=req['id'], reason='later', due_at=20_000))
    result = scheduler.observation(store.snapshot())['progression'][0]
    assert result['state'] == 'stalled' and result['reason'] == 'no_work'
    assert len(result['timers']) == 1


def test_stalled_event_lifecycle_follows_recovery(tmp_path):
    store, scheduler, req = health_fixture(tmp_path)
    store.link_task(req['id'], 'task')
    scheduler.sync_health(store.snapshot(), [{'id': 'task', 'status': 'triage'}], [])
    assert len([e for e in store.pending_events() if e['kind'] == 'host.stalled']) == 1
    scheduler.sync_health(store.snapshot(), [
        {'id': 'task', 'status': 'running', 'last_heartbeat_at': 9_999}], [])
    assert not [e for e in store.pending_events() if e['kind'] == 'host.stalled']
    restarted = Scheduling(Store(store.path), lambda: 10_000)
    restarted.sync_health(store.snapshot(), [{'id': 'task', 'status': 'triage'}], [])
    assert len([e for e in store.pending_events() if e['kind'] == 'host.stalled']) == 1


def test_paused_requirement_is_not_stalled_and_suppresses_wakes(tmp_path):
    store, scheduler, req = health_fixture(tmp_path)
    store.link_task(req['id'], 'task')
    wake = scheduler.call(dict(action='schedule', requirement_id=req['id'],
                               reason='later', due_at=20_000))
    pause = store.create_requirement_command('pause', req['id'], 'user asked', 'Stop briefly')
    store.confirm_requirement_command(pause['id'], 'pause-confirmation')
    data = store.snapshot()

    health = scheduler.health(
        data, [{'id': 'task', 'status': 'running', 'last_heartbeat_at': 1}], [])[0]
    assert health['state'] == 'paused'
    assert health['reason'] == 'requirement_paused'
    scheduler.sync_health(data, [{'id': 'task', 'status': 'running',
                                  'last_heartbeat_at': 1}], [])
    assert not [e for e in store.pending_events() if e['kind'] == 'host.stalled']
    observation = scheduler.observation(data, [{'id': 'task', 'status': 'running',
                                                'last_heartbeat_at': 1}], [])
    assert observation['liveness']['due_at'] is None
    assert observation['progression'][0]['state'] == 'paused'
    scheduler.scan()
    assert next(item for item in scheduler.call({'action': 'list'})
                if item['id'] == wake['id'])['state'] == 'suppressed'

    resume = store.create_requirement_command('resume', req['id'], 'user ready', 'Continue')
    store.confirm_requirement_command(resume['id'], 'resume-confirmation')
    assert scheduler.observation(store.snapshot(), [], [])['liveness']['due_at'] is not None
