"""End-to-end local tool journeys use public hooks and the native-board adapter contract."""
import json
from types import SimpleNamespace

import pytest

import ancor
from ancor.store import Store
from ancor.service import Service
from ancor.operations import Operations
from test_operations import Board, contract


def registered(tmp_path, monkeypatch, role='ancor-interactor', board=None):
    board = board or Board()
    monkeypatch.setattr('ancor.operations.NativeBoard', lambda *a, **k: board)
    monkeypatch.setattr('ancor.service.Service.native_board', property(lambda self: board))
    tools, hooks = {}, {}
    config = {'store_path': str(tmp_path / 'store.db'), 'board': 'test', 'human_target': 'test:user'}
    ctx = SimpleNamespace(profile_name=role, get_config=lambda key, default=None: config.get(key, default),
                          register_tool=lambda **kw: tools.update({kw['name']: kw}),
                          register_hook=lambda key, fn: hooks.update({key: fn}), register_platform_handler=lambda *a: None)
    ancor.register(ctx)
    return ctx, tools, hooks, board


def call(tools, name, args):
    result = json.loads(tools[name]['handler'](args))
    assert 'error' not in result, result
    return result


def confirm(hooks, proposal, call_id):
    args = proposal['clarify_args']
    assert hooks['pre_tool_call']('clarify', args, session_id='human-session', tool_call_id=call_id) is None
    q = args['questions'][0]
    result = {'outcome': 'submitted', 'responses': [{'question': q['question'], 'choices_offered': q['choices'],
              'status': 'answered', 'user_response': q['choices'][1]}]}
    hooks['post_tool_call']('clarify', args, result, session_id='human-session', tool_call_id=call_id)


def create_requirement(tools, hooks):
    p = call(tools, 'ancor_change', {'action': 'propose', 'input': 'Deliver report without personal data',
             'operations': [{'op': 'create', 'title': 'Report', 'fields': {
                 'outcome': ['Deliver report'], 'constraint': ['No personal data']}}]})
    confirm(hooks, p, 'create')
    return p['requirement_id']


def test_public_structured_journey_and_local_revision(tmp_path, monkeypatch):
    ctx, human, hooks, board = registered(tmp_path, monkeypatch)
    rid = create_requirement(human, hooks)
    _, coordinator, _, _ = registered(tmp_path, monkeypatch, 'ancor-coordinator', board)
    store = Store(ctx.get_config('store_path'))
    context = call(human, 'ancor_get_context', {'requirement_id': rid})
    versions = [p['version_id'] for ps in context['content']['fields'].values() for p in ps]
    t = call(coordinator, 'ancor_board', {'action': 'create', 'requirement_id': rid, 'key': 'report',
             'title': 'Write report', 'contract': contract(), 'generation': 1, 'point_versions': versions})
    tid = t['task_id']
    assert board.show(tid)['status'] == 'blocked'  # created safely before releasing work
    call(coordinator, 'ancor_reconcile', {'action': 'review', 'requirement_id': rid, 'generation': 1,
         'decisions': [{'task_id': tid, 'decision': 'unaffected', 'reason': 'new card matches requirements'}],
         'coverage_reason': 'Report card covers goal and privacy constraint'})
    call(coordinator, 'ancor_board', {'action': 'unblock', 'task_id': tid, 'reason': 'reviewed'})
    assert board.show(tid)['status'] == 'ready'
    p = call(human, 'ancor_change', {'action': 'propose', 'requirement_id': rid, 'base': store.get_requirement(rid),
             'input': 'Use Markdown', 'operations': [{'op': 'append', 'field': 'outcome', 'body': 'Markdown output'}]})
    confirm(hooks, p, 'format')
    assert board.show(tid)['status'] == 'ready'  # no blanket archive/block
    assert store.get_requirement(rid)['generation'] == 2
    call(coordinator, 'ancor_reconcile', {'action': 'review', 'requirement_id': rid, 'generation': 2,
         'decisions': [{'task_id': tid, 'decision': 'revise', 'reason': 'output format changed'}],
         'coverage_reason': 'Revise existing report card to cover new format'})
    call(coordinator, 'ancor_board', {'action': 'block', 'task_id': tid, 'reason': 'apply format change'})
    content = store.content(rid)
    versions = [p['version_id'] for ps in content['fields'].values() for p in ps]
    call(coordinator, 'ancor_board', {'action': 'edit', 'task_id': tid, 'generation': 2, 'point_versions': versions,
         'contract': contract(outcome='Write Markdown report')})
    call(coordinator, 'ancor_reconcile', {'action': 'finish', 'requirement_id': rid, 'generation': 2,
         'task_id': tid, 'evidence': 'Contract now specifies Markdown'})
    call(coordinator, 'ancor_board', {'action': 'unblock', 'task_id': tid, 'reason': 'updated'})
    board.tasks[tid]['task']['status'] = 'done'
    evidence = {'generation': 2, 'coverage': [{'version_id': v, 'evidence': 'checked report'} for v in versions],
                'verification': ['independent read'], 'artifacts': ['report.md'], 'task_ids': [tid]}
    call(coordinator, 'ancor_acceptance', {'action': 'verify', 'requirement_id': rid, 'evidence': evidence})
    p = call(human, 'ancor_requirement', {'action': 'accept', 'requirement_id': rid})
    confirm(hooks, p, 'accept')
    assert store.get_requirement(rid)['status'] == 'accepted'
    assert len(store.list_changes(rid)['items']) == 3


def test_tools_enforce_role_boundary_and_note_versions(tmp_path, monkeypatch):
    ctx, human, hooks, board = registered(tmp_path, monkeypatch)
    rid = create_requirement(human, hooks)
    _, coordinator, _, _ = registered(tmp_path, monkeypatch, 'ancor-coordinator', board)
    forbidden = json.loads(coordinator['ancor_change']['handler']({'action': 'propose', 'operations': []}))
    assert forbidden['code'] == 'forbidden'
    forbidden = json.loads(human['ancor_question']['handler']({'action': 'create'}))
    assert forbidden['code'] == 'forbidden'
    note = call(coordinator, 'ancor_note', {'action': 'append', 'requirement_id': rid, 'body': 'hypothesis'})
    note = call(coordinator, 'ancor_note', {'action': 'replace', 'requirement_id': rid, 'note_id': note['id'],
                'expected_revision': 1, 'body': 'verified observation'})
    stale = json.loads(coordinator['ancor_note']['handler']({'action': 'invalidate', 'requirement_id': rid,
                       'note_id': note['id'], 'expected_revision': 1}))
    assert stale['code'] == 'state_conflict'
    history = call(coordinator, 'ancor_note', {'action': 'history', 'requirement_id': rid, 'note_id': note['id']})
    assert [v['body'] for v in history['items']] == ['hypothesis', 'verified observation']
    assert Store(ctx.get_config('store_path')).get_requirement(rid)['generation'] == 1


def test_public_question_answer_persists_in_current_context(tmp_path, monkeypatch):
    ctx, human, hooks, board = registered(tmp_path, monkeypatch)
    rid = create_requirement(human, hooks)
    _, coordinator, _, _ = registered(tmp_path, monkeypatch, 'ancor-coordinator', board)
    call(coordinator, 'ancor_question', {'action': 'create', 'id': 'where', 'requirement_id': rid,
         'body': 'Where is the input?', 'reason': 'Cannot discover the location'})
    store = Store(ctx.get_config('store_path'))
    proposal = call(human, 'ancor_change', {'action': 'propose', 'requirement_id': rid, 'input': '/data/input.csv',
          'base': store.get_requirement(rid), 'operations': [
              {'op': 'append', 'field': 'context', 'body': 'Input: /data/input.csv'},
              {'op': 'reply', 'question_id': 'where', 'revision': 1, 'body': '/data/input.csv'},
              {'op': 'resolve_question', 'question_id': 'where', 'revision': 1, 'reason': 'Location recorded'}]})
    confirm(hooks, proposal, 'answer')
    store.ack_events([e['id'] for e in store.pending_events()])
    context = call(human, 'ancor_get_context', {'requirement_id': rid})
    assert context['content']['fields']['context'][0]['body'] == 'Input: /data/input.csv'
    assert context['questions']['count'] == 0


def test_live_worker_cannot_be_edited_even_when_card_blocked(tmp_path, monkeypatch):
    import os
    ctx, human, hooks, board = registered(tmp_path, monkeypatch)
    rid = create_requirement(human, hooks)
    store = Store(ctx.get_config('store_path'))
    coordinator_ctx = SimpleNamespace(profile_name='ancor-coordinator', get_config=lambda k, d=None: d)
    operations = Operations(coordinator_ctx, store, board)
    versions = [p['version_id'] for ps in store.content(rid)['fields'].values() for p in ps]
    result = operations.call({'action': 'create', 'requirement_id': rid, 'key': 'k', 'title': 'T',
                              'contract': contract(), 'generation': 1, 'point_versions': versions})
    tid = result['task_id']
    board.tasks[tid]['runs'] = [{'worker_pid': os.getpid(), 'ended_at': 1}]
    with pytest.raises(RuntimeError, match='quiescence'):
        operations.call({'action': 'edit', 'task_id': tid, 'contract': contract(outcome='Changed'),
                         'generation': 1, 'point_versions': versions})


def test_coordinator_notes_not_exposed_by_human_history(tmp_path, monkeypatch):
    ctx, human, hooks, board = registered(tmp_path, monkeypatch)
    rid = create_requirement(human, hooks)
    store = Store(ctx.get_config('store_path'))
    store.add_note(rid, 'internal hypothesis')
    nid = store.list_changes(rid)['items'][0]['id']
    history = call(human, 'ancor_change', {'action': 'history', 'requirement_id': rid})
    assert history['count'] == 1
    result = json.loads(human['ancor_change']['handler']({'action': 'get', 'change_id': nid}))
    assert result['code'] == 'forbidden'
