import importlib
import json
from types import SimpleNamespace

import pytest

from ancor.board import NativeBoard
from ancor.store import Store


class Board:
    link = staticmethod(NativeBoard.link)

    def __init__(self):
        self.tasks = {}
        self.calls = []

    def list(self):
        return [v['task'].copy() for v in self.tasks.values()]

    def show(self, task_id):
        return self.tasks[task_id]['task'].copy()

    def create(self, requirement_id, revision, key, **kwargs):
        self.calls.append(('create', kwargs))
        link = dict(requirement_id=requirement_id, revision=revision, key=key)
        for value in self.tasks.values():
            if self.link(value['task']) == link:
                return value['task'].copy()
        task = dict(id=str(len(self.tasks) + 1), status='running', **kwargs)
        task['body'] = 'ANCOR-LINK:' + json.dumps(link) + '\n' + kwargs['body']
        self.tasks[task['id']] = dict(task=task, comments=[], runs=[], events=[], parents=[], children=[])
        return task.copy()

    def _call(self, *args, json_output=True):
        self.calls.append(args)
        if args[0] == 'show':
            return self.tasks[args[1]]
        if args[0] == 'comment':
            self.tasks[args[1]]['comments'].append({'author': 'ancor-leader', 'body': args[2]})
            return ''
        if args[0] == 'unblock':
            self.tasks[args[1]]['task']['status'] = 'ready'
            return ''
        raise AssertionError(args)

    def block(self, task_id, reason):
        self.calls.append(('block', task_id, reason))
        self.tasks[task_id]['task']['status'] = 'blocked'
        return self.show(task_id)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = store.confirm(store.create_draft('Build thing', 'Tests pass')['id'], 'test-human')
    ctx = SimpleNamespace(profile_name='ancor-leader', get_config=lambda key, default=None: default)
    return ctx, store, Board(), req


def operations(setup):
    # The explicit assertion makes the initial RED a missing-feature failure.
    import importlib.util
    assert importlib.util.find_spec('ancor.operations'), 'scoped Operations is missing'
    return importlib.import_module('ancor.operations').Operations(*setup[:3])


def create_args(req, **extra):
    return dict(action='create', requirement_id=req['id'], key='first', title='Implement', body='Work details', **extra)


def test_create_links_readback_and_replays_without_requirement_card(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    result = op.call(create_args(req))
    assert result['task']['assignee'] == 'ancor-worker'
    assert req['brief'] in result['task']['body']
    assert req['acceptance_criteria'] in result['task']['body']
    assert op.call(create_args(req))['task']['id'] == result['task']['id']
    assert len(board.tasks) == 1
    assert store.snapshot()['task_links'] == [{'requirement_id': req['id'], 'task_id': result['task']['id']}]
    assert store.get_requirement(req['id'])['status'] == 'active'


def test_scoped_read_and_mutations(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    task = op.call(create_args(req))['task']
    tid = task['id']
    assert op.call({'action': 'list'})['tasks'][0]['task']['id'] == tid
    assert 'runs' in op.call({'action': 'show', 'task_id': tid})
    assert op.call({'action': 'block', 'task_id': tid, 'reason': 'Need answer'})['task']['status'] == 'blocked'
    answer = op.call({'action': 'comment', 'task_id': tid, 'body': 'User answered: use blue'})
    assert answer['comments'][-1]['body'] == 'User answered: use blue'
    assert op.call({'action': 'unblock', 'task_id': tid, 'reason': 'Answer recorded'})['task']['status'] == 'ready'
    assert ('unblock', tid, '--reason', 'Answer recorded') in board.calls
    with pytest.raises(ValueError, match='unlinked'):
        op.call({'action': 'block', 'task_id': 'unrelated', 'reason': 'no'})


def test_create_requires_active_requirement_and_leader(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    ctx.profile_name = 'ancor-worker'
    with pytest.raises(PermissionError):
        op.call(create_args(req))
    ctx.profile_name = 'ancor-leader'
    store.accept(req['id'], 'test evidence')
    with pytest.raises(ValueError, match='active'):
        op.call(create_args(req))
    assert not board.tasks


@pytest.mark.parametrize('target', ['replacement', 'independent'])
def test_invalidated_worker_does_not_gate_new_work(setup, monkeypatch, target):
    import os
    ctx, store, board, req = setup
    op = operations(setup)
    tid = op.call(create_args(req))['task']['id']
    board.tasks[tid]['runs'] = [{'id': 1, 'worker_pid': 123, 'ended_at': None}]
    replacement = store.confirm(store.create_draft('Changed', 'New tests', req['id'])['id'], 'test-human-2')
    if target == 'independent':
        replacement = store.confirm(store.create_draft('Independent', 'Other tests')['id'], 'test-human-3')
    monkeypatch.setattr(os, 'kill', lambda pid, signal: None)
    assert op.call(create_args(replacement))['task']['id'] != tid
    assert board.show(tid)['status'] == 'running'
    assert not any(call[0] == 'block' for call in board.calls)
    # Stopping affected work remains an explicit coordinator decision.
    assert op.call({'action': 'block', 'task_id': tid, 'reason': 'Conflicting work'})['task']['status'] == 'blocked'
    with pytest.raises(ValueError, match='active'):
        op.call(create_args(req))
    board.tasks[tid]['runs'] = []
    with pytest.raises(ValueError, match='active'):
        op.call({'action': 'unblock', 'task_id': tid, 'reason': 'unsafe'})


def test_unblock_is_not_gated_by_other_invalidated_workers(setup, monkeypatch):
    import os
    ctx, store, board, req = setup
    op = operations(setup)
    old_tid = op.call(create_args(req))['task']['id']
    other = store.confirm(store.create_draft('Other', 'Other tests')['id'], 'human:other')
    tid = op.call(create_args(other))['task']['id']
    op.call({'action': 'block', 'task_id': tid, 'reason': 'Wait for answer'})
    store.confirm(store.create_draft('Changed', 'New tests', req['id'])['id'], 'human:change')
    board.tasks[old_tid]['runs'] = [{'worker_pid': 123, 'ended_at': None}]
    monkeypatch.setattr(os, 'kill', lambda pid, signal: None)
    board.calls.clear()
    assert op.call({'action': 'unblock', 'task_id': tid, 'reason': 'Answered'})['task']['status'] == 'ready'
    assert board.show(old_tid)['status'] == 'running'
    assert not any(call[0] == 'block' for call in board.calls)


def test_native_metadata_recovers_crash_before_store_link(setup):
    ctx, store, board, req = setup
    task = board.create(req['id'], 1, 'first', title='existing', body='already dispatched', assignee='ancor-worker')
    op = operations(setup)
    assert op.call({'action': 'comment', 'task_id': task['id'], 'body': 'Recovered'})['task']['id'] == task['id']
    assert store.snapshot()['task_links'][0]['task_id'] == task['id']


@pytest.mark.parametrize('action', ['list', 'show'])
def test_reads_use_persisted_links_without_reconciliation(setup, monkeypatch, action):
    ctx, store, board, req = setup
    op = operations(setup)
    linked = op.call(create_args(req))['task']
    orphan = board.create(req['id'], 1, 'orphan', title='Orphan', body='Unlinked', assignee='ancor-worker')
    before = store.snapshot()

    def forbidden(*args, **kwargs):
        pytest.fail('foreground reads must not reconcile native task links')

    monkeypatch.setattr(board, 'list', forbidden)
    monkeypatch.setattr(store, 'link_task', forbidden)
    if action == 'list':
        assert [item['task']['id'] for item in op.call({'action': action})['tasks']] == [linked['id']]
    else:
        assert op.call({'action': action, 'task_id': linked['id']})['task']['id'] == linked['id']
        with pytest.raises(ValueError, match='unlinked'):
            op.call({'action': action, 'task_id': orphan['id']})
    assert store.snapshot() == before


def test_unblock_cannot_overlap_its_previous_executor(setup, monkeypatch):
    import os
    op = operations(setup)
    tid = op.call(create_args(setup[3]))['task']['id']
    setup[2].tasks[tid]['task']['status'] = 'blocked'
    setup[2].tasks[tid]['runs'] = [{'worker_pid': 234, 'ended_at': 1}]
    monkeypatch.setattr(os, 'kill', lambda pid, signal: None)
    with pytest.raises(RuntimeError, match='quiescen'):
        op.call({'action': 'unblock', 'task_id': tid, 'reason': 'retry'})
    assert setup[2].show(tid)['status'] == 'blocked'


def test_create_refuses_replayed_task_with_wrong_assignee(setup):
    ctx, store, board, req = setup
    board.create(req['id'], 1, 'first', title='wrong', body='wrong', assignee='another-profile')
    with pytest.raises(RuntimeError, match='assignee'):
        operations(setup).call(create_args(req))


@pytest.mark.parametrize('existing_assignee', [None, 'special-worker', 'ancor-worker'])
def test_create_uses_configured_worker_and_verifies_readback(setup, existing_assignee):
    ctx, store, board, req = setup
    ctx.get_config = lambda key, default=None: 'special-worker' if key == 'worker_profile' else default
    if existing_assignee is not None:
        board.create(req['id'], 1, 'first', title='existing', body='existing', assignee=existing_assignee)
    if existing_assignee == 'ancor-worker':
        with pytest.raises(RuntimeError, match='assignee'):
            operations(setup).call(create_args(req))
    else:
        result = operations(setup).call(create_args(req))
        assert result['task']['assignee'] == 'special-worker'
    assert [call for call in board.calls if call[0] == 'create'][-1][1]['assignee'] == 'special-worker'


def test_native_parser_accepts_exact_mutation_contracts(monkeypatch):
    import argparse
    from pathlib import Path
    source = Path('/home/user/.hermes/hermes-agent')
    if not source.exists():
        pytest.skip('installed native parser unavailable')
    monkeypatch.syspath_prepend(str(source))
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    args = parser.parse_args(['kanban', '--board', 'ancor', 'unblock', 't1', '--reason', 'Answer recorded'])
    assert args.task_ids == ['t1'] and args.reason == 'Answer recorded'
    args = parser.parse_args(['kanban', '--board', 'ancor', 'comment', 't1', 'User answer', '--author', 'ancor-leader'])
    assert args.text == ['User answer'] and args.author == 'ancor-leader'
