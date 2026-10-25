"""Real process crashes/restarts; scripted coordinator, never model/transport E2E."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from ancor.host import Host
from ancor.notification import NotificationOutbox
from ancor.store import Store
from test_host import Board, Context


ROOT = Path(__file__).parents[1]
SCRIPT = r'''
import asyncio, json, os, sys, time
from pathlib import Path
from ancor.host import Host
from ancor.store import Store
class Context:
    def get_config(self, key, default=None):
        return {'store_path': sys.argv[1]}.get(key, default)
class Board:
    def list(self): return []
host = Host(Context(), board=Board())
if sys.argv[2] == 'accept-crash':
    store = host.store
    req = store.confirm(store.create_draft('Deliver', title='Deliver artifact')['id'], 'human:exact')
    store.submit_acceptance(req['id'], 'preserved verification evidence')
    store.ack_events([e['id'] for e in store.pending_events()])
    Path(sys.argv[3]).write_text(req['id'])
    while True: time.sleep(.01)
else:
    child = ('import json,sys; from pathlib import Path; from ancor.store import Store; '
             'p=json.loads(sys.argv[2].split("\\n",1)[1]); '
             'Path(sys.argv[1]).exists() or Path(sys.argv[1]).write_text(json.dumps(p)); '
             's=Store(sys.argv[3]); s.ack_events([e["id"] for e in p["events"]])')
    host.coordinator.command = lambda p: [sys.executable, '-c', child, sys.argv[3], p, sys.argv[1]]
    assert asyncio.run(host.coordinator.tick()) == 'ran'
    host.coordinator.next_attempt = 0
    outcomes = [asyncio.run(host.coordinator.tick()) for _ in range(5)]
    assert outcomes.count('ran') <= 1 and outcomes[-1] in {'idle', 'backoff'}
'''


def test_replacement_transition_survives_restart_and_advances(tmp_path):
    from ancor.transitions import advance_request

    path = tmp_path / 'store.db'
    ctx = Context(path)
    store = Store(path)
    old = store.confirm(store.create_draft('old')['id'], 'human:old')
    new = store.confirm(store.create_draft('new', old['id'])['id'], 'human:new')
    store.link_task(old['id'], 'old-worker')
    store.link_task(new['id'], 'new-worker')
    board = Board({'id': 'old-worker', 'status': 'running'})
    board.tasks.append({'id': 'new-worker', 'status': 'running',
                        'last_heartbeat_at': time.time()})
    store.ack_events([e['id'] for e in store.pending_events()])

    host = Host(ctx, board=board, db_path=tmp_path / 'native.db')
    host.scan()
    snapshot = host.wake_packet()
    requests = {row['id']: row for row in snapshot['requests']['items']}
    assert requests[old['id']]['target_status'] == 'invalidated'
    assert requests[new['id']]['target_status'] == 'active'
    assert {row['reason'] for row in snapshot['progression']} == {'transition_pending'}
    assert snapshot['invalidated_tasks']['items'] == []
    assert host.pending(32)

    result = advance_request(store, board, old['id'])
    assert result['outcome'] == 'completed'
    assert store.get_request(old['id'])['status'] == 'invalidated'
    assert store.get_request(new['id'])['status'] == 'active'
    assert board.tasks[0]['status'] == 'archived'
    assert board.guards == []

    store.ack_events([e['id'] for e in store.pending_events()])
    for _ in range(3):
        host.scan()
        assert host.pending(32) == []
    assert host.wake_packet()['invalidated_tasks']['items'] == []


def test_invalidated_drift_is_visible_without_mutating_native_work(tmp_path):
    from ancor.transitions import advance_request

    path = tmp_path / 'store.db'
    ctx = Context(path)
    store = Store(path)
    old = store.confirm(store.create_draft('old')['id'], 'human:old')
    store.confirm(store.create_draft('new', old['id'])['id'], 'human:new')
    store.link_task(old['id'], 'old-worker')
    board = Board({'id': 'old-worker', 'status': 'running'})
    advance_request(store, board, old['id'])
    host = Host(ctx, board=board, db_path=tmp_path / 'native.db')
    host.tasks = board.list()
    unresolved = host.invalidated_tasks(store.snapshot())
    assert unresolved == []

    store.link_task(old['id'], 'drifted-worker')
    host.tasks = [*board.tasks, {'id': 'drifted-worker', 'status': 'running'}]
    unresolved = host.invalidated_tasks(store.snapshot())
    assert unresolved[0]['request_id'] == old['id']
    assert unresolved[0]['task_id'] == 'drifted-worker'
    assert host.tasks[-1]['status'] == 'running'


def wait_file(path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(.01)
    raise AssertionError(f'process did not reach checkpoint: {path}')


def test_accepted_before_enqueue_crash_wakes_once_without_resubmission(tmp_path):
    path = tmp_path / 'store.db'
    checkpoint, output = tmp_path / 'accepted', tmp_path / 'prompt'
    process = subprocess.Popen([sys.executable, '-c', SCRIPT, str(path), 'accept-crash', str(checkpoint)], cwd=ROOT)
    try:
        wait_file(checkpoint)
        process.kill()
        process.wait(timeout=5)
        result = subprocess.run([sys.executable, '-c', SCRIPT, str(path), 'restart', str(output)],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(output.read_text())
        assert payload['events'][0]['kind'] == 'host.recovery'
        request = payload['snapshot']['requests']['items'][0]
        assert request['status'] == 'awaiting_acceptance'
        assert 'brief' not in request
        restarted = Store(path)
        assert restarted.get_request(request['id'])['confirmation_reference'] == 'human:exact'
        assert restarted.acceptances()[0]['payload']['evidence'] == 'preserved verification evidence'
        assert not Path(str(path) + '.notifications.db').exists(), 'host must not invent a send'
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize('state', ['pending', 'sending', 'uncertain', 'sent'])
def test_notification_restart_preserves_state_and_does_not_send(tmp_path, state):
    path = tmp_path / 'store.db'
    store = Store(path)
    req = store.confirm(store.create_draft('deliver')['id'], 'human')
    store.submit_acceptance(req['id'], 'evidence')
    store.ack_events([e['id'] for e in store.pending_events()])
    box = NotificationOutbox(str(path) + '.notifications.db', coordinator_profile='ancor-coordinator',
                             target='test:destination')
    identifier = 'accept:' + req['id']
    box.enqueue(identifier, 'completion', 'Exact result', actor='ancor-coordinator')
    if state in {'sending', 'uncertain', 'sent'}:
        assert box.claim_next()['state'] == 'sending'
    if state == 'uncertain':
        from ancor.notification import SenderError
        box.mark_uncertain(identifier, SenderError('timeout'))
    elif state == 'sent':
        box.mark_sent(identifier, {'success': True})
    host = Host(Context(path), board=Board())
    events = host.pending(32)
    store.ack_events([e['id'] for e in events])
    for _ in range(5):
        assert host.pending(32) == []
    assert box.get(identifier)['state'] == state
    assert len(box.list()) == 1


def test_sent_question_is_not_completion_delivery(tmp_path):
    path = tmp_path / 'store.db'
    store = Store(path)
    req = store.confirm(store.create_draft('deliver')['id'], 'human')
    store.submit_acceptance(req['id'], 'evidence')
    store.ack_events([e['id'] for e in store.pending_events()])
    box = NotificationOutbox(str(path) + '.notifications.db', coordinator_profile='ancor-coordinator',
                             target='test:destination')
    identifier = 'accept:' + req['id']
    box.enqueue(identifier, 'question', 'Is this suitable?', actor='ancor-coordinator')
    assert box.claim_next()['state'] == 'sending'
    box.mark_sent(identifier, {'success': True})
    box.respond(identifier, 'yes', 'human:answer')
    host = Host(Context(path), board=Board())
    requests = host.wake_packet()['requests']['items']
    assert [row['id'] for row in requests] == [req['id']]
    assert requests[0]['status'] == 'awaiting_acceptance'
    assert [e['kind'] for e in host.pending(32)] == ['host.recovery']


def test_interrupted_host_and_coordinator_resume_without_duplicate_flight(tmp_path):
    path = tmp_path / 'store.db'
    store = Store(path)
    req = store.confirm(store.create_draft('deliver')['id'], 'human:original')
    checkpoint = tmp_path / 'coordinator'
    child_script = ('import os,sys,time; from pathlib import Path; from ancor.store import Store; '
                    's=Store(sys.argv[1]); s.ack_events([e["id"] for e in s.pending_events()]); '
                    'Path(sys.argv[2]).write_text(str(os.getpid())); time.sleep(60)')
    host_script = SCRIPT.split("if sys.argv[2] == 'accept-crash':")[0] + (
        '\nhost.coordinator.command=lambda p:[sys.executable,"-c",sys.argv[4],sys.argv[1],sys.argv[3]]'
        '\nasyncio.run(host.coordinator.tick())')
    process = subprocess.Popen([sys.executable, '-c', host_script, str(path), 'crash', str(checkpoint), child_script])
    coordinator = None
    try:
        wait_file(checkpoint)
        coordinator = int(checkpoint.read_text())
        process.kill()
        process.wait(timeout=5)
        restarted = Host(Context(path), board=Board())
        assert asyncio.run(restarted.coordinator.tick()) == 'busy'
        assert not store.pending_events()  # surviving child still owns the flock
        os.kill(coordinator, 9)
        output = tmp_path / 'recovered'
        result = subprocess.run([sys.executable, '-c', SCRIPT, str(path), 'restart', str(output)],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        data = json.loads(output.read_text())
        assert 'host.recovery' in [e['kind'] for e in data['events']]
        assert [row['id'] for row in data['snapshot']['requests']['items']] == [req['id']]
        assert len(store.snapshot()['requests']) == 1
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if coordinator:
            try:
                os.kill(coordinator, 9)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('acknowledged', [False, True])
def test_confirmed_demand_restart_requires_no_resubmit(tmp_path, acknowledged):
    path = tmp_path / 'store.db'
    store = Store(path)
    req = store.confirm(store.create_draft('deliver')['id'], 'human:original')
    if acknowledged:
        store.ack_events([e['id'] for e in store.pending_events()])
    output = tmp_path / 'prompt'
    result = subprocess.run([sys.executable, '-c', SCRIPT, str(path), 'restart', str(output)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text())
    assert [row['id'] for row in data['snapshot']['requests']['items']] == [req['id']]
    kinds = [e['kind'] for e in data['events']]
    assert ('request.activated' in kinds) == (not acknowledged)
    assert data['snapshot']['conditions']['count'] == 1
    assert data['snapshot']['conditions']['items'][0]['reason'] == 'no_work'
    assert ('host.recovery' in kinds) == acknowledged


def test_native_completion_written_during_downtime_replayed_once(tmp_path, monkeypatch):
    # Real installed native list_events interface; fixture writer is NOT a worker model.
    from ancor.board import NativeBoard
    native = Path('/home/user/.hermes/hermes-agent')
    if not native.is_dir():
        pytest.skip('installed native source unavailable')
    monkeypatch.syspath_prepend(str(native))
    path, db = tmp_path / 'store.db', tmp_path / 'native.db'
    store = Store(path)
    req = store.confirm(store.create_draft('deliver')['id'], 'human:original')
    store.link_task(req['id'], 'worker')
    store.ack_events([e['id'] for e in store.pending_events()])
    writer = (
        'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); '
        'c.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT,payload TEXT,created_at INTEGER,run_id INTEGER)"); '
        'c.execute("INSERT INTO task_events VALUES (1,?, ?, ?, 1, NULL)", '
        '("worker","completed",sys.argv[2])); c.commit()')
    subprocess.run([sys.executable, '-c', writer, str(db), json.dumps({'result': 'artifact verified'})], check=True)
    board = Board()
    board.events = NativeBoard.events
    restarted = Host(Context(path), board=board, db_path=db)
    restarted.scan()
    events = restarted.pending(32)
    assert [e['kind'] for e in events] == ['native.completed']
    assert events[0]['payload']['payload']['result'] == 'artifact verified'
    store.ack_events([e['id'] for e in events])
    restarted.scan()
    assert restarted.pending(32) == []
    assert restarted.state()['task_cursors'] == {'worker': 1}
