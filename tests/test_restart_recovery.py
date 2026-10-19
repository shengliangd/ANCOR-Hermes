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
    req = store.confirm(store.create_draft('Deliver', 'verified artifact')['id'], 'human:exact')
    store.accept(req['id'], 'preserved verification evidence')
    store.ack_events([e['id'] for e in store.pending_events()])
    Path(sys.argv[3]).write_text(req['id'])
    while True: time.sleep(.01)
else:
    child = ('import json,sys; from pathlib import Path; from ancor.store import Store; '
             'p=json.loads(sys.argv[2].split("\\n",1)[1]); '
             'Path(sys.argv[1]).write_text(json.dumps(p)); '
             's=Store(sys.argv[3]); s.ack_events([e["id"] for e in p["events"]])')
    host.coordinator.command = lambda p: [sys.executable, '-c', child, sys.argv[3], p, sys.argv[1]]
    assert asyncio.run(host.coordinator.tick()) == 'ran'
    host.coordinator.next_attempt = 0
    for _ in range(5): assert asyncio.run(host.coordinator.tick()) == 'idle'
'''


@pytest.mark.parametrize('status,retired,run,expected', [
    ('running', False, None, True),
    ('ready', False, None, True),
    ('todo', False, None, True),
    ('review', False, None, True),
    ('triage', False, None, True),
    ('blocked', False, None, True),
    ('blocked', True, {'ended_at': None, 'worker_pid': None}, True),
    ('blocked', True, {'ended_at': 'ended', 'worker_pid': os.getpid()}, True),
    ('done', False, None, False),
    ('blocked', True, None, False),
])
def test_invalidated_orphan_restart(tmp_path, status, retired, run, expected):
    from copy import deepcopy
    from ancor.retirement import Retirement

    path = tmp_path / 'store.db'
    ctx = Context(path)
    store = Store(path)
    old = store.confirm(store.create_draft('old', 'checked')['id'], 'human:old')
    new = store.confirm(store.create_draft('new', 'checked', old['id'])['id'], 'human:new')
    store.link_task(old['id'], 'old-worker')
    board = Board({'id': 'old-worker', 'status': 'blocked'})
    detail = {'task': board.tasks[0], 'runs': []}
    calls = []
    def show(*args):
        calls.append(args)
        assert args == ('show', 'old-worker'), 'recovery must never mutate native work'
        return deepcopy(detail)
    board._call = show
    if retired:
        ctx.profile_name = 'ancor-coordinator'
        Retirement(ctx, store, board).retire(old['id'], 'old-worker', 'Superseded')
        ctx.profile_name = 'default'
    detail['task']['status'] = status
    detail['runs'] = [run] if run else []
    before = deepcopy(detail)
    store.accept(new['id'], 'verified replacement')
    box = NotificationOutbox(str(path) + '.notifications.db', coordinator_profile='ancor-coordinator',
                             target='test:destination', sender=lambda *a: {'success': True})
    box.enqueue('accept:' + new['id'], 'completion', 'Delivered replacement', actor='ancor-coordinator')
    box.deliver('accept:' + new['id'], actor='ancor-coordinator')
    store.ack_events([e['id'] for e in store.pending_events()])

    host = Host(ctx, board=board, db_path=tmp_path / 'native.db')
    host.scan()
    events = host.pending(32)
    assert [e['kind'] for e in events] == (['host.recovery'] if expected else [])
    unresolved = host.snapshot()['invalidated_tasks']
    if expected:
        assert unresolved[0]['requirement'] == store.get_requirement(old['id'])
        assert unresolved[0]['task_id'] == 'old-worker'
        assert unresolved[0]['reason']
        assert 'coordinator decides' in events[0]['payload']['reason']
    else:
        assert unresolved == []
    store.ack_events([e['id'] for e in events])
    for _ in range(3):
        host.scan()
        assert host.pending(32) == []
    assert detail == before
    assert board.guards == []


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
        assert payload['snapshot']['requirements'][0]['status'] == 'accepted'
        assert payload['snapshot']['requirements'][0]['confirmation_reference'] == 'human:exact'
        assert payload['snapshot']['acceptances'][0]['payload']['evidence'] == 'preserved verification evidence'
        assert not Path(str(path) + '.notifications.db').exists(), 'host must not invent a send'
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize('state', ['pending', 'sending', 'uncertain', 'sent'])
def test_notification_restart_preserves_state_and_does_not_send(tmp_path, state):
    path = tmp_path / 'store.db'
    store = Store(path)
    req = store.confirm(store.create_draft('deliver', 'checked')['id'], 'human')
    store.accept(req['id'], 'evidence')
    store.ack_events([e['id'] for e in store.pending_events()])
    box = NotificationOutbox(str(path) + '.notifications.db', coordinator_profile='ancor-coordinator',
                             target='test:destination', sender=lambda *a: {'success': True})
    identifier = 'accept:' + req['id']
    box.enqueue(identifier, 'completion', 'Exact result', actor='ancor-coordinator')
    if state == 'sent':
        box.deliver(identifier, actor='ancor-coordinator')
    elif state in {'sending', 'uncertain'}:
        script = (
            'import sys,time; from pathlib import Path; from ancor.notification import NotificationOutbox; '
            'checkpoint=Path(sys.argv[3]); '
            '\ndef sender(*args):\n checkpoint.touch()\n'
            ' if sys.argv[4] == "uncertain": raise TimeoutError("test")\n'
            ' while True: time.sleep(.01)\n'
            'box=NotificationOutbox(sys.argv[1],coordinator_profile="ancor-coordinator",'
            'target="test:destination",sender=sender); '
            'box.deliver(sys.argv[2],actor="ancor-coordinator")')
        checkpoint = tmp_path / 'sending'
        child = subprocess.Popen([sys.executable, '-c', script, box.path, identifier, str(checkpoint), state])
        try:
            wait_file(checkpoint)
            if state == 'uncertain':
                assert child.wait(timeout=5) == 0
            else:
                child.kill()
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
    host = Host(Context(path), board=Board())
    events = host.pending(32)
    assert bool(events) == (state != 'sent')
    if state != 'sent':
        assert host.snapshot()['notifications'][0]['state'] == state
        store.ack_events([e['id'] for e in events])
    for _ in range(5):
        assert host.pending(32) == []
    assert box.get(identifier)['state'] == state
    assert len(box.list()) == 1


def test_sent_question_is_not_completion_delivery(tmp_path):
    path = tmp_path / 'store.db'
    store = Store(path)
    req = store.confirm(store.create_draft('deliver', 'checked')['id'], 'human')
    store.accept(req['id'], 'evidence')
    store.ack_events([e['id'] for e in store.pending_events()])
    box = NotificationOutbox(str(path) + '.notifications.db', coordinator_profile='ancor-coordinator',
                             target='test:destination', sender=lambda *a: {'success': True})
    identifier = 'accept:' + req['id']
    box.enqueue(identifier, 'question', 'Is this suitable?', actor='ancor-coordinator')
    box.deliver(identifier, actor='ancor-coordinator')
    box.respond(identifier, 'yes', 'human:answer')
    host = Host(Context(path), board=Board())
    assert host.snapshot()['requirements'] == [store.get_requirement(req['id'])]
    assert [e['kind'] for e in host.pending(32)] == ['host.recovery']


def test_interrupted_host_and_coordinator_resume_without_duplicate_flight(tmp_path):
    path = tmp_path / 'store.db'
    store = Store(path)
    req = store.confirm(store.create_draft('deliver', 'checked')['id'], 'human:original')
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
        assert data['events'][0]['kind'] == 'host.recovery'
        assert data['snapshot']['requirements'] == [req]
        assert len(store.snapshot()['requirements']) == 1
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
    req = store.confirm(store.create_draft('deliver', 'checked')['id'], 'human:original')
    if acknowledged:
        store.ack_events([e['id'] for e in store.pending_events()])
    output = tmp_path / 'prompt'
    result = subprocess.run([sys.executable, '-c', SCRIPT, str(path), 'restart', str(output)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text())
    assert data['snapshot']['requirements'] == [req]
    assert data['events'][0]['kind'] == ('host.recovery' if acknowledged else 'requirement.confirmed')


def test_native_completion_written_during_downtime_replayed_once(tmp_path, monkeypatch):
    # Real installed native list_events interface; fixture writer is NOT a worker model.
    from ancor.board import NativeBoard
    native = Path('/home/user/.hermes/hermes-agent')
    if not native.is_dir():
        pytest.skip('installed native source unavailable')
    monkeypatch.syspath_prepend(str(native))
    path, db = tmp_path / 'store.db', tmp_path / 'native.db'
    store = Store(path)
    req = store.confirm(store.create_draft('deliver', 'checked')['id'], 'human:original')
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
