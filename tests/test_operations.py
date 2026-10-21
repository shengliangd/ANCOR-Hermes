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

    dependency = NativeBoard.dependency
    edit = NativeBoard.edit
    def revise(self, task_id, reason):
        self.calls.append(('revise', task_id, reason))
        self.tasks[task_id]['comments'].append({'author': 'ancor-coordinator', 'body': reason.strip()})
        self.tasks[task_id]['task']['status'] = 'ready'
        return self.tasks[task_id]

    def list(self):
        return [v['task'].copy() for v in self.tasks.values()]

    def show(self, task_id):
        return self.tasks[task_id]['task'].copy()

    def create(self, requirement_id, revision, key, **kwargs):
        self.calls.append(('create', kwargs.copy()))
        link = dict(requirement_id=requirement_id, revision=revision, key=key)
        for value in self.tasks.values():
            if self.link(value['task']) == link:
                return value['task'].copy()
        parent_ids = kwargs.pop('parent_ids', [])
        hold = kwargs.pop('hold', False)
        task = dict(id=str(len(self.tasks) + 1), status='blocked' if hold else 'running', **kwargs)
        task['body'] = 'ANCOR-LINK:' + json.dumps(link) + '\n' + kwargs['body']
        self.tasks[task['id']] = dict(task=task, comments=[], runs=[], events=[], parents=parent_ids, children=[])
        return task.copy()

    def _call(self, *args, json_output=True):
        self.calls.append(args)
        if args[0] == 'show':
            return self.tasks[args[1]]
        if args[0] == 'edit':
            for flag in args[2:]:
                key, value = flag[2:].split('=', 1)
                self.tasks[args[1]]['task'][key] = int(value) if key == 'priority' else value
            return ''
        if args[0] in {'link', 'unlink'}:
            parents = self.tasks[args[2]]['parents']
            if args[0] == 'link':
                parents.append(args[1])
            else:
                parents.remove(args[1])
            return ''
        if args[0] == 'comment':
            self.tasks[args[1]]['comments'].append({'author': 'ancor-coordinator', 'body': args[2]})
            return ''
        if args[0] in {'unblock', 'revise'}:
            self.tasks[args[1]]['task']['status'] = 'ready'
            return ''
        raise AssertionError(args)

    def block(self, task_id, reason):
        self.calls.append(('block', task_id, reason))
        self.tasks[task_id]['task']['status'] = 'blocked'
        return self.show(task_id)

    def archive(self, task_id, reason):
        self.calls.append(('archive', task_id, reason))
        self.tasks[task_id]['task']['status'] = 'archived'
        return self.show(task_id)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = store.confirm(store.create_draft('Build thing', 'Tests pass')['id'], 'test-human')
    ctx = SimpleNamespace(profile_name='ancor-coordinator', get_config=lambda key, default=None: default)
    return ctx, store, Board(), req


def operations(setup):
    # The explicit assertion makes the initial RED a missing-feature failure.
    import importlib.util
    assert importlib.util.find_spec('ancor.operations'), 'scoped Operations is missing'
    return importlib.import_module('ancor.operations').Operations(*setup[:3])


def contract(**extra):
    value = dict(objective='Implement the requested result', scope='Only the named artifact',
                 inputs=['/tmp/input.txt'], dependencies=[], artifacts=['/tmp/result.txt'],
                 verification=['Run the exact acceptance check'], constraints=['Keep exact values'],
                 stop_conditions=['Required input is missing'])
    value.update(extra)
    return value


def create_args(req, **extra):
    value = dict(action='create', requirement_id=req['id'], key='first', title='Implement',
                 body='Work details', contract=contract())
    value.update(extra)
    return value


def test_create_with_atomic_parents_and_hold(setup):
    op = operations(setup)
    parent = op.call(create_args(setup[3]))['task_id']
    args = create_args(setup[3], parent_ids=[parent], hold=True)
    args['key'] = 'dependent'
    result = op.call(args)
    kwargs = [call[1] for call in setup[2].calls if call[0] == 'create'][-1]
    assert kwargs['parent_ids'] == [parent] and kwargs['hold'] is True
    assert result['status'] == 'blocked'
    assert result['parents'] == [parent]


@pytest.mark.parametrize('extra', [{'parent_ids': ['foreign']}, {'parent_ids': 'x'}, {'hold': 1}])
def test_create_rejects_invalid_planning_inputs(setup, extra):
    with pytest.raises(ValueError):
        operations(setup).call(create_args(setup[3], **extra))
    assert not setup[2].tasks


def test_create_links_readback_and_replays_without_requirement_card(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    result = op.call(create_args(req))
    shown = op.call({'action': 'show', 'task_id': result['task_id']})
    assert result['assignee'] == 'ancor-worker'
    assert contract()['objective'] in shown['body']
    assert 'Keep exact values' in shown['body']
    assert req['acceptance_criteria'] not in shown['body']
    assert op.call(create_args(req))['task_id'] == result['task_id']
    assert len(board.tasks) == 1
    assert store.snapshot()['task_links'] == [{'requirement_id': req['id'], 'task_id': result['task_id']}]
    assert store.get_requirement(req['id'])['status'] == 'active'


def test_create_derives_task_text_from_contract_when_body_omitted(setup):
    args = create_args(setup[3])
    del args['body']
    result = operations(setup).call(args)
    shown = operations(setup).call({'action': 'show', 'task_id': result['task_id']})
    assert 'Task: ' + contract()['objective'] in shown['body']


def test_create_requires_a_complete_self_contained_contract(setup):
    op = operations(setup)
    args = create_args(setup[3])
    del args['contract']
    with pytest.raises(ValueError, match='contract'):
        op.call(args)
    args = create_args(setup[3], contract=contract(artifacts=[]))
    with pytest.raises(ValueError, match='artifacts'):
        op.call(args)
    args = create_args(setup[3], contract=contract(verification=['  ']))
    with pytest.raises(ValueError, match='verification'):
        op.call(args)
    assert not setup[2].tasks


def test_create_renders_structured_contract(setup):
    result = operations(setup).call(create_args(
        setup[3], contract=contract(inputs=['exact input'], dependencies=['Task A done'],
                                    artifacts=['artifact path'], verification=['check result'],
                                    constraints=['preserve casing'], stop_conditions=['missing approval'])))
    body = operations(setup).call({'action': 'show', 'task_id': result['task_id']})['body']
    for value in ('Implement the requested result', 'Only the named artifact', 'exact input',
                  'Task A done', 'artifact path', 'check result', 'preserve casing',
                  'missing approval'):
        assert value in body


def test_scoped_dependencies_across_active_requirements(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    parent = op.call(create_args(req))['task_id']
    other = store.confirm(store.create_draft('Other', 'Pass')['id'], 'human:other')
    child = op.call(create_args(other))['task_id']
    board.tasks[child]['task']['status'] = 'blocked'
    args = dict(action='link', parent_id=parent, child_id=child)
    assert op.call(args)['parents'] == [parent]
    assert op.call(dict(args, action='unlink'))['parents'] == []
    with pytest.raises(ValueError, match='unlinked'):
        op.call(dict(args, parent_id='foreign'))
    store.accept(other['id'], 'evidence')
    with pytest.raises(ValueError, match='active'):
        op.call(args)


def test_unlink_obsolete_prerequisite_from_active_child(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    parent = op.call(create_args(req))['task_id']
    other = store.confirm(store.create_draft('Other', 'Pass')['id'], 'human:other')
    child = op.call(create_args(other))['task_id']
    args = dict(action='link', parent_id=parent, child_id=child)
    op.call(args)
    store.confirm(store.create_draft('Changed', 'New tests', req['id'])['id'], 'human:change')
    assert op.call(dict(args, action='unlink'))['parents'] == []
    with pytest.raises(ValueError, match='active'):
        op.call(args)
    store.accept(other['id'], 'evidence')
    with pytest.raises(ValueError, match='active'):
        op.call(dict(args, action='unlink'))
    with pytest.raises(ValueError, match='unlinked'):
        op.call(dict(args, action='unlink', parent_id='foreign'))


def test_revise_is_explicit_reviewed_triage_recovery(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    tid = op.call(create_args(req))['task_id']
    board.tasks[tid]['task']['status'] = 'triage'
    assert op.call({'action': 'show', 'task_id': tid})['status'] == 'triage'
    assert not any(call[0] == 'revise' for call in board.calls)
    result = op.call({'action': 'revise', 'task_id': tid, 'reason': 'Coordinator reviewed revised plan'})
    assert result['status'] == 'ready'
    assert ('revise', tid, 'Coordinator reviewed revised plan') in board.calls


@pytest.mark.parametrize('fault', ['inactive', 'unlinked', 'running', 'blocked', 'archived', 'empty_reason', 'executor'])
def test_revise_rejects_unsafe_recovery(setup, monkeypatch, fault):
    import os
    ctx, store, board, req = setup
    op = operations(setup)
    tid = op.call(create_args(req))['task_id']
    board.tasks[tid]['task']['status'] = 'triage'
    args = dict(action='revise', task_id=tid, reason='Reviewed')
    if fault == 'inactive':
        store.accept(req['id'], 'evidence')
    elif fault == 'unlinked':
        args['task_id'] = 'foreign'
    elif fault in {'running', 'blocked'}:
        board.tasks[tid]['task']['status'] = fault
    elif fault == 'archived':
        board.tasks[tid]['task']['archived_at'] = 1
    elif fault == 'empty_reason':
        args['reason'] = '  '
    elif fault == 'executor':
        board.tasks[tid]['runs'] = [{'worker_pid': 234, 'ended_at': 1}]
        monkeypatch.setattr(os, 'kill', lambda pid, signal: None)
    board.calls.clear()
    with pytest.raises((ValueError, RuntimeError)):
        op.call(args)
    assert not any(call[0] == 'revise' for call in board.calls)


def test_promote_is_not_exposed(setup):
    with pytest.raises(ValueError, match='unknown board action'):
        operations(setup).call(dict(action='promote', task_id='unused', reason='Reviewed'))


def test_edit_keeps_requirement_context_and_association(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    created = op.call(create_args(req))
    task = op.call({'action': 'show', 'task_id': created['task_id']})
    args = dict(action='edit', task_id=created['task_id'], title='Refined', body='New task details', priority=0,
                contract=contract())
    receipt = op.call(args)
    edited = op.call({'action': 'show', 'task_id': receipt['task_id']})
    assert edited['title'] == 'Refined' and edited['priority'] == 0
    assert NativeBoard.link(edited) == NativeBoard.link(task)
    assert 'Task: New task details' in edited['body']
    assert contract()['objective'] in edited['body'] and 'Keep exact values' in edited['body']
    assert req['acceptance_criteria'] not in edited['body']
    assert edited['assignee'] == 'ancor-worker'
    store.accept(req['id'], 'evidence')
    with pytest.raises(ValueError, match='active'):
        op.call(args)


def test_edit_body_requires_a_full_contract(setup):
    op = operations(setup)
    task = op.call(create_args(setup[3]))
    with pytest.raises(ValueError, match='contract'):
        op.call(dict(action='edit', task_id=task['task_id'], body='Changed'))
    receipt = op.call(dict(action='edit', task_id=task['task_id'], body='Changed',
                           contract=contract(objective='Replacement objective')))
    edited = op.call({'action': 'show', 'task_id': receipt['task_id']})
    assert 'Replacement objective' in edited['body'] and 'Task: Changed' in edited['body']


@pytest.mark.parametrize('priority', [True, 1.5, '3', None])
def test_edit_rejects_non_integer_priority(setup, priority):
    op = operations(setup)
    tid = op.call(create_args(setup[3]))['task_id']
    with pytest.raises(ValueError, match='integer'):
        op.call(dict(action='edit', task_id=tid, priority=priority))


def test_configured_coordinator_is_used_for_native_cli(setup):
    ctx = setup[0]
    ctx.profile_name = 'custom-coordinator'
    ctx.get_config = lambda key, default=None: {'coordinator_profile': 'custom-coordinator', 'board': 'custom-board'}.get(key, default)
    op = importlib.import_module('ancor.operations').Operations(ctx, setup[1])
    assert op.board.prefix[:6] == ['hermes', '-p', 'custom-coordinator', 'kanban', '--board', 'custom-board']


@pytest.mark.parametrize('action', ['link', 'unlink', 'edit', 'revise'])
def test_new_mutations_are_coordinator_only(setup, action):
    setup[0].profile_name = 'ancor-worker'
    with pytest.raises(PermissionError, match='coordinator'):
        operations(setup).call({'action': action})
    assert setup[2].calls == []


def test_interactor_board_access_is_read_only(setup):
    ctx, store, board, req = setup
    coordinator = operations(setup)
    tid = coordinator.call(create_args(req))['task_id']
    ctx.profile_name = 'ancor-interactor'
    op = operations(setup)
    assert op.call({'action': 'list'})['items'][0]['id'] == tid
    assert op.call({'action': 'show', 'task_id': tid})['id'] == tid
    with pytest.raises(PermissionError, match='coordinator'):
        op.call(dict(action='block', task_id=tid, reason='No'))


@pytest.mark.parametrize('changes', [{}, {'title': ''}, {'body': ''}])
def test_empty_edits_do_not_mutate(setup, changes):
    op = operations(setup)
    tid = op.call(create_args(setup[3]))['task_id']
    setup[2].calls.clear()
    with pytest.raises(ValueError):
        op.call(dict(action='edit', task_id=tid, **changes))
    assert not any(call[0] == 'edit' for call in setup[2].calls)


def test_scoped_read_and_mutations(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    task = op.call(create_args(req))
    tid = task['task_id']
    assert op.call({'action': 'list'})['items'][0]['id'] == tid
    assert 'runs' in op.call({'action': 'show', 'task_id': tid})
    assert op.call({'action': 'block', 'task_id': tid, 'reason': 'Need answer'})['status'] == 'blocked'
    answer = op.call({'action': 'comment', 'task_id': tid, 'body': 'User answered: use blue'})
    assert answer['changed'] == ['comment']
    shown = op.call({'action': 'show', 'task_id': tid})
    assert shown['comments']['items'][-1]['body'] == 'User answered: use blue'
    assert op.call({'action': 'unblock', 'task_id': tid, 'reason': 'Answer recorded'})['status'] == 'ready'
    assert ('unblock', tid, '--reason', 'Answer recorded') in board.calls
    with pytest.raises(ValueError, match='unlinked'):
        op.call({'action': 'block', 'task_id': 'unrelated', 'reason': 'no'})


def test_show_and_history_bound_large_task_sections(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    tid = op.call(create_args(req))['task_id']
    board.tasks[tid]['comments'] = [
        {'id': index, 'author': 'ancor-coordinator', 'body': f'comment {index}'}
        for index in range(1, 26)]
    board.tasks[tid]['events'] = [
        {'id': index, 'kind': 'test.event'} for index in range(1, 26)]
    board.tasks[tid]['runs'] = [
        {'id': index, 'ended_at': index} for index in range(1, 11)]

    shown = op.call({'action': 'show', 'task_id': tid})
    assert shown['comments']['count'] == 25
    assert [row['id'] for row in shown['comments']['items']] == list(range(16, 26))
    assert shown['events']['returned'] == 10
    assert [row['id'] for row in shown['runs']['items']] == list(range(6, 11))

    page = op.call({'action': 'history', 'task_id': tid,
                    'kind': 'comments', 'limit': 5})
    assert page['count'] == 25 and page['returned'] == 5 and page['truncated'] is True
    assert [row['id'] for row in page['items']] == [21, 22, 23, 24, 25]
    older = op.call({'action': 'history', 'task_id': tid, 'kind': 'comments',
                     'limit': 5, 'before_id': 21})
    assert older['count'] == 20 and older['truncated'] is True
    assert [row['id'] for row in older['items']] == [16, 17, 18, 19, 20]


def test_create_requires_active_requirement_and_coordinator(setup):
    ctx, store, board, req = setup
    op = operations(setup)
    ctx.profile_name = 'ancor-worker'
    with pytest.raises(PermissionError):
        op.call(create_args(req))
    ctx.profile_name = 'ancor-coordinator'
    store.accept(req['id'], 'test evidence')
    with pytest.raises(ValueError, match='active'):
        op.call(create_args(req))
    assert not board.tasks


@pytest.mark.parametrize('target', ['replacement', 'independent'])
def test_invalidated_worker_is_archived_before_new_work(setup, monkeypatch, target):
    import os
    from ancor.transitions import advance_requirement
    ctx, store, board, req = setup
    op = operations(setup)
    tid = op.call(create_args(req))['task_id']
    board.tasks[tid]['runs'] = [{'id': 1, 'worker_pid': 123, 'ended_at': None}]
    replacement = store.confirm(store.create_draft('Changed', 'New tests', req['id'])['id'], 'test-human-2')
    if target == 'independent':
        replacement = store.confirm(store.create_draft('Independent', 'Other tests')['id'], 'test-human-3')
    monkeypatch.setattr(os, 'kill', lambda pid, signal: None)
    result = advance_requirement(store, board, req['id'])
    assert result['outcome'] == 'completed'
    assert op.call(create_args(replacement))['task_id'] != tid
    assert board.show(tid)['status'] == 'archived'
    assert not any(call[0] == 'block' for call in board.calls)
    assert any(call[0] == 'archive' for call in board.calls)
    with pytest.raises(ValueError, match='active'):
        op.call(create_args(req))
    board.tasks[tid]['runs'] = []
    with pytest.raises(ValueError, match='active'):
        op.call({'action': 'unblock', 'task_id': tid, 'reason': 'unsafe'})


def test_unblock_is_not_gated_by_other_invalidated_workers(setup, monkeypatch):
    import os
    ctx, store, board, req = setup
    op = operations(setup)
    old_tid = op.call(create_args(req))['task_id']
    other = store.confirm(store.create_draft('Other', 'Other tests')['id'], 'human:other')
    tid = op.call(create_args(other))['task_id']
    op.call({'action': 'block', 'task_id': tid, 'reason': 'Wait for answer'})
    store.confirm(store.create_draft('Changed', 'New tests', req['id'])['id'], 'human:change')
    board.tasks[old_tid]['runs'] = [{'worker_pid': 123, 'ended_at': None}]
    monkeypatch.setattr(os, 'kill', lambda pid, signal: None)
    board.calls.clear()
    assert op.call({'action': 'unblock', 'task_id': tid, 'reason': 'Answered'})['status'] == 'ready'
    assert board.show(old_tid)['status'] == 'running'
    assert not any(call[0] == 'block' for call in board.calls)


def test_native_metadata_recovers_crash_before_store_link(setup):
    ctx, store, board, req = setup
    task = board.create(req['id'], 1, 'first', title='existing', body='already dispatched', assignee='ancor-worker')
    op = operations(setup)
    assert op.call({'action': 'comment', 'task_id': task['id'], 'body': 'Recovered'})['task_id'] == task['id']
    assert store.snapshot()['task_links'][0]['task_id'] == task['id']


@pytest.mark.parametrize('action', ['list', 'show'])
def test_reads_use_persisted_links_without_reconciliation(setup, monkeypatch, action):
    ctx, store, board, req = setup
    op = operations(setup)
    linked = op.call(create_args(req))
    orphan = board.create(req['id'], 1, 'orphan', title='Orphan', body='Unlinked', assignee='ancor-worker')
    before = store.snapshot()

    def forbidden(*args, **kwargs):
        pytest.fail('foreground reads must not reconcile native task links')

    monkeypatch.setattr(store, 'link_task', forbidden)
    if action == 'list':
        assert [item['id'] for item in op.call({'action': action})['items']] == [linked['task_id']]
    else:
        assert op.call({'action': action, 'task_id': linked['task_id']})['id'] == linked['task_id']
        with pytest.raises(ValueError, match='unlinked'):
            op.call({'action': action, 'task_id': orphan['id']})
    assert store.snapshot() == before


def test_unblock_cannot_overlap_its_previous_executor(setup, monkeypatch):
    import os
    op = operations(setup)
    tid = op.call(create_args(setup[3]))['task_id']
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
        assert result['assignee'] == 'special-worker'
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
    args = parser.parse_args(['kanban', '--board', 'ancor', 'comment', 't1', 'User answer', '--author', 'ancor-coordinator'])
    assert args.text == ['User answer'] and args.author == 'ancor-coordinator'
