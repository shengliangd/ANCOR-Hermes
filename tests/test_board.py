import json
import subprocess
import pytest
import sqlite3
from pathlib import Path


def test_installed_native_cli_roundtrip_isolated(tmp_path):
    import os
    import sys
    source = Path('/home/user/.hermes/hermes-agent')
    if not source.exists():
        pytest.skip('installed Hermes source unavailable')
    home = tmp_path / 'home'
    home.mkdir()
    import site
    paths = [str(source), site.getusersitepackages(), *site.getsitepackages()]
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'HERMES_HOME': str(home / '.hermes'), 'PYTHONPATH': os.pathsep.join(paths)}
    # This is an authorized isolated native probe. Do not inherit the parent
    # agent's delegated-child mutation fences into the probe process.
    script = 'import argparse,sys; from hermes_cli.kanban_parser import build_parser; from hermes_cli.kanban import kanban_command; p=argparse.ArgumentParser(); build_parser(p.add_subparsers()); sys.exit(kanban_command(p.parse_args(sys.argv[1:])))'
    probe = subprocess.run([str(source / '.venv/bin/python'), '-c', 'from hermes_cli.kanban import kanban_command'], env=env, capture_output=True, text=True)
    if probe.returncode and 'ModuleNotFoundError' in probe.stderr:
        pytest.skip('isolated installed CLI missing dependency: ' + probe.stderr.splitlines()[-1])
    def run(argv, **kwargs):
        return subprocess.run([str(source / '.venv/bin/python'), '-c', script, *argv[3:]], **kwargs)
    board = NativeBoard('default', run=run, env=env)
    first = board.create('r1', 1, 'step', title='isolated test', body='no execution', assignee='ancor-test-worker')
    again = board.create('r1', 1, 'step', title='isolated test', body='no execution', assignee='ancor-test-worker')
    assert first['id'] == again['id']
    assert len(board.list()) == 1
    assert board.guard({'r1': 2}) == [first['id']]
    assert board.show(first['id'])['status'] == 'blocked'
    parent = board.create('r2', 1, 'prerequisite', title='Prerequisite', body='independent demand', assignee='ancor-test-worker')
    # Establish an actual native claim before testing the running-child guard.
    if board.show(parent['id'])['status'] == 'ready':
        board._call('claim', parent['id'], json_output=False)
    assert board.show(parent['id'])['status'] == 'running'
    with pytest.raises(subprocess.CalledProcessError):
        board.dependency('link', first['id'], parent['id'])
    assert board.dependency('link', parent['id'], first['id'])['parents'] == [parent['id']]
    board._call('unblock', first['id'], '--reason', 'dependency prepared', json_output=False)
    assert board.show(first['id'])['status'] == 'todo'
    changed = board.edit(first['id'], title='Refined work', priority=7)
    assert changed['task']['priority'] == 7
    assert board.link(changed['task']) == board.link(first)
    assert board.dependency('unlink', parent['id'], first['id'])['task']['status'] == 'ready'
    assert board.dependency('link', parent['id'], first['id'])['task']['status'] == 'todo'
    with pytest.raises(subprocess.CalledProcessError):
        board.dependency('link', first['id'], first['id'])
    board.block(parent['id'], 'park prerequisite')
    with pytest.raises(subprocess.CalledProcessError):
        board.dependency('link', first['id'], parent['id'])  # cycle


def test_native_events_read_only_and_bounded(tmp_path, monkeypatch):
    source = Path('/home/user/.hermes/hermes-agent')
    if not source.exists():
        pytest.skip('installed Hermes source unavailable')
    monkeypatch.syspath_prepend(str(source))
    path = tmp_path / 'native.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER, run_id INTEGER)')
        conn.executemany('INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?)', [(1, 'a', 'created', '{}', 10, None), (2, 'a', 'completed', '{}', 11, 1), (3, 'b', 'blocked', '{}', 12, 2)])
    before = path.read_bytes()
    rows = NativeBoard.events(path, ['a', 'b'], after=1, limit=1)
    assert [e['id'] for e in rows] == [2]
    assert rows[0]['kind'] == 'completed'
    assert path.read_bytes() == before
    assert not (tmp_path / 'missing.db').exists()
    with pytest.raises(sqlite3.OperationalError):
        NativeBoard.events(tmp_path / 'missing.db', ['a'])


def test_guard_blocks_stale_but_not_unrelated(tmp_path):
    tasks = [
        {'id': 'stale', 'status': 'running', 'body': 'ANCOR-LINK:{"requirement_id":"r1","revision":1,"key":"a"}'},
        {'id': 'fresh', 'status': 'running', 'body': 'ANCOR-LINK:{"requirement_id":"r1","revision":2,"key":"b"}'},
        {'id': 'other', 'status': 'running', 'body': 'unmanaged'},
    ]
    blocked = []
    def run(argv, **kwargs):
        if 'list' in argv:
            result = tasks
        elif 'block' in argv:
            ident = argv[argv.index('block') + 1]
            blocked.append(ident)
            next(t for t in tasks if t['id'] == ident)['status'] = 'blocked'
            return subprocess.CompletedProcess(argv, 0, '', '')
        else:
            result = {'task': next(t for t in tasks if t['id'] == argv[-2])}
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), '')
    board = NativeBoard('ancor', run=run)
    assert board.guard({'r1': 2}) == ['stale']
    assert board.guard({'r1': 2}) == []
    assert blocked == ['stale']
    assert board.link({'body': 'ANCOR-LINK:[]'}) is None

def test_guard_defers_unsupported_states_and_isolates_failures():
    board = NativeBoard('test')
    tasks = [{'id': status, 'status': status,
              'body': 'ANCOR-LINK:{"requirement_id":"old","revision":1,"key":"x"}'}
             for status in ('todo', 'review', 'running', 'ready', 'triage')]
    board.list = lambda: tasks
    calls = []
    def block(task_id, reason):
        calls.append(task_id)
        if task_id == 'running':
            raise RuntimeError('transition raced')
        return {'status': 'blocked'}
    board.block = block
    assert board.guard({}) == ['ready']
    assert calls == ['running', 'ready']
    assert board.guard_state == {'deferred': ['todo', 'review', 'triage'],
                                 'failed': ['running'], 'stopped': ['ready']}
    tasks[0]['status'] = 'ready'
    assert board.guard({}) == ['todo', 'ready']


from ancor.board import NativeBoard


@pytest.mark.parametrize('action', ['link', 'unlink'])
def test_dependency_cli_and_readback(action):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        result = {'task': {'id': 'child'}, 'parents': ['parent'] if action == 'link' else []}
        return subprocess.CompletedProcess(argv, 0, json.dumps(result) if 'show' in argv else '', '')
    board = NativeBoard('ancor', run=run)
    assert board.dependency(action, 'parent', 'child')['task']['id'] == 'child'
    assert calls[0][-3:] == [action, 'parent', 'child']
    assert calls[1][-3:] == ['show', 'child', '--json']


def test_edit_cli_readback_preserves_native_contract():
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({'task': {'title': '--literal', 'priority': 0}}) if 'show' in argv else '', '')
    board = NativeBoard('ancor', run=run)
    assert board.edit('t1', title='--literal', priority=0)['task']['priority'] == 0
    assert calls[0][-4:] == ['edit', 't1', '--title=--literal', '--priority=0']


def test_create_durable_link_and_explicit_cli_scope(tmp_path):
    calls = []
    task = {'id': 't1', 'body': '', 'status': 'running'}
    def run(argv, **kwargs):
        calls.append(argv)
        if 'create' in argv:
            task['body'] = argv[argv.index('--body') + 1]
            result = task
        elif 'show' in argv:
            result = {'task': task}
        else:
            result = []
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), '')
    board = NativeBoard('ancor', profile='ancor-coordinator', run=run)
    result = board.create('r1', 2, 'step1', title='Do it', body='details', assignee='ancor-worker')
    assert result['id'] == 't1'
    assert board.link(result) == {'requirement_id': 'r1', 'revision': 2, 'key': 'step1'}
    create = next(c for c in calls if 'create' in c)
    assert create[:6] == ['hermes', '-p', 'ancor-coordinator', 'kanban', '--board', 'ancor']
    assert '--idempotency-key' in create
    assert calls[-1][-3:] == ['show', 't1', '--json']
