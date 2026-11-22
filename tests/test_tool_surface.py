"""Direct v2 tool surface, role boundaries and protocol compatibility."""
import json
from pathlib import Path
from types import SimpleNamespace

import yaml
from test_operations import Board, contract

import ancor
from ancor.host import Host
from ancor.request_contract import DESCRIPTIONS, FIELDS
from ancor.store import Store
from ancor.tool_surface import rewrite_legacy_references

ROOT = Path(__file__).parents[1]


def registered(tmp_path, monkeypatch, role, *, mode='v2', board=None, admin=False):
    board = board or Board()
    monkeypatch.setattr('ancor.operations.NativeBoard', lambda *args, **kwargs: board)
    monkeypatch.setattr('ancor.service.Service.native_board', property(lambda self: board))
    tools, hooks = {}, {}
    config = {
        'store_path': str(tmp_path / 'store.db'), 'board': 'test',
        'human_target': 'test:user', 'tool_api': mode, 'admin_tools': admin,
    }
    ctx = SimpleNamespace(
        profile_name=role,
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **kwargs: tools.update({kwargs['name']: kwargs}),
        register_hook=lambda key, fn: hooks.update({key: fn}),
        register_platform_handler=lambda *args: None,
    )
    ancor.register(ctx)
    return ctx, tools, hooks, board


def call(tools, name, args, **kwargs):
    return json.loads(tools[name]['handler'](args, **kwargs))


def confirm(hooks, proposal, call_id):
    args = proposal['clarify_args']
    assert hooks['pre_tool_call']('clarify', args, session_id='human', tool_call_id=call_id) is None
    question = args['questions'][0]
    result = {'outcome': 'submitted', 'responses': [{
        'question': question['question'], 'choices_offered': question['choices'],
        'status': 'answered', 'user_response': question['choices'][1]}]}
    hooks['post_tool_call']('clarify', args, result, session_id='human', tool_call_id=call_id)


def test_v2_surface_is_role_scoped_and_mutations_have_direct_schemas(tmp_path, monkeypatch):
    interactor_ctx, interactor, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-interactor', mode='v2')
    coordinator_ctx, coordinator, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-coordinator', mode='v2')
    _, worker, _, _ = registered(tmp_path, monkeypatch, 'ancor-worker', mode='v2')

    assert 'ancor_request_draft' not in interactor
    assert 'ancor_request_confirm' not in interactor
    assert 'ancor_change_propose' in interactor
    assert 'ancor_task_create' not in interactor
    assert 'ancor_request_advance' in coordinator
    assert 'ancor_change_propose' not in coordinator
    assert 'ancor_task_block' in coordinator
    assert not worker

    for name in ('ancor_board', 'ancor_note', 'ancor_wake', 'ancor_request',
                 'ancor_acceptance', 'ancor_reconcile', 'ancor_get_context'):
        assert name not in interactor
        assert name not in coordinator

    block = coordinator['ancor_task_block']['schema']['parameters']
    assert block['required'] == ['task_id', 'reason']
    assert set(block['properties']) == {'task_id', 'reason'}
    assert 'action' not in block['properties']

    missing = call(coordinator, 'ancor_task_block', {'task_id': 'task'})
    assert missing['code'] == 'invalid_request'
    assert missing['missing_fields'] == ['reason']
    assert 'action=' not in missing['next_step']

    interactor_ctx.profile_name = 'ancor-coordinator'
    forbidden = call(interactor, 'ancor_change_propose', {
        'input': 'Wrong role',
        'operations': [{'op': 'create', 'title': 'Wrong role',
                        'fields': {'outcome': ['Must not create']}}]})
    assert forbidden['code'] == 'forbidden'
    coordinator_ctx.profile_name = 'ancor-interactor'
    forbidden = call(coordinator, 'ancor_task_block', {
        'task_id': 'task', 'reason': 'Must not block'})
    assert forbidden['code'] == 'forbidden'


def test_v2_schema_contract_is_closed_and_manifested(tmp_path, monkeypatch):
    _, interactor, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-interactor', mode='v2')
    _, coordinator, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-coordinator', mode='v2', admin=True)
    manifest = yaml.safe_load((ROOT / 'ancor/plugin.yaml').read_text())
    provided = set(manifest['provides_tools'])
    all_tools = {**interactor, **coordinator}
    names = set(all_tools)
    assert names <= provided
    assert not {'ancor_board', 'ancor_note', 'ancor_wake', 'ancor_request',
                'ancor_acceptance', 'ancor_reconcile'} & names
    for name in names:
        schema = all_tools[name]['schema']['parameters']
        assert schema['additionalProperties'] is False, name
        assert set(schema['required']) <= set(schema['properties']), name
        assert 'action' not in schema['properties'], name
    evidence = all_tools['ancor_delivery_verify']['schema']['parameters'][
        'properties']['evidence']
    assert evidence['additionalProperties'] is False
    assert all_tools['ancor_task_history']['schema']['parameters'][
        'properties']['limit']['maximum'] == 100

    native_read = call(interactor, 'ancor_overview', {}, task_id='native-task')
    assert 'error' not in native_read
    native_tasks = call(coordinator, 'ancor_task_list', {}, task_id='native-task')
    assert 'error' not in native_tasks

    operations = all_tools['ancor_change_propose']['schema']['parameters'][
        'properties']['operations']['items']['oneOf']
    create = next(op for op in operations if op['properties']['op']['const'] == 'create')
    fields = create['properties']['fields']['properties']
    assert tuple(fields) == FIELDS
    assert {field: schema['description'] for field, schema in fields.items()} == DESCRIPTIONS


def test_v2_single_call_wrapper_is_normalized_and_batches_are_rejected(tmp_path, monkeypatch):
    _, tools, _, _ = registered(tmp_path, monkeypatch, 'ancor-interactor', mode='v2')
    def events():
        return Store(tmp_path / 'store.db').snapshot()['pending_events']

    wrapped = {
        'calls': [{'name': 'ancor_signal_propose', 'arguments': {
            'text': 'Wrapped signal'}}],
        'name': 'ancor_signal_propose',
    }
    result = call(tools, 'ancor_signal_propose', wrapped)
    assert result['clarify_args']
    assert result['id']
    outer_only = call(tools, 'ancor_signal_propose', {
        'calls': [{'text': 'Outer-name signal'}],
        'name': 'ancor_signal_propose',
    })
    assert outer_only['id'] != result['id']
    inner_only = call(tools, 'ancor_signal_propose', {
        'calls': [{'name': 'ancor_signal_propose', 'arguments': {
            'text': 'Inner-name signal'}}],
    })
    assert inner_only['id'] not in {result['id'], outer_only['id']}

    before = events()
    unnamed = call(tools, 'ancor_signal_propose', {
        'calls': [{'arguments': {'text': 'Must not write'}}]})
    assert unnamed['code'] == 'protocol_error'
    assert events() == before
    extra = call(tools, 'ancor_signal_propose', {
        'calls': [{'arguments': {'text': 'Must not write'},
                   'unexpected': True}],
        'name': 'ancor_signal_propose'})
    assert extra['code'] == 'protocol_error'
    assert events() == before
    null_arguments = call(tools, 'ancor_signal_propose', {
        'calls': [{'name': 'ancor_signal_propose', 'arguments': None}],
        'name': 'ancor_signal_propose'})
    assert null_arguments['code'] == 'protocol_error'
    mismatch = call(tools, 'ancor_signal_propose', {
        'calls': [{'name': 'ancor_task_block', 'arguments': {}}],
        'name': 'ancor_signal_propose'})
    assert mismatch['code'] == 'protocol_error'
    assert events() == before

    rejected = call(tools, 'ancor_signal_propose', {
        'calls': [
            {'name': 'ancor_signal_propose', 'arguments': {'text': 'Must not write'}},
            {'name': 'ancor_signal_propose', 'arguments': {'text': 'Must not write'}},
        ],
        'name': 'ancor_signal_propose'})
    assert rejected['code'] == 'protocol_error'
    assert events() == before


def test_v2_direct_tools_drive_structured_request_and_task(tmp_path, monkeypatch):
    _, human_tools, hooks, board = registered(
        tmp_path, monkeypatch, 'ancor-interactor', mode='v2')
    _, coordinator_tools, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-coordinator', mode='v2', board=board)
    proposal = call(human_tools, 'ancor_change_propose', {
        'input': 'Deliver a verified report',
        'operations': [{'op': 'create', 'title': 'Report', 'fields': {
            'outcome': ['Deliver a report'], 'constraint': ['Keep it concise']}}],
    })
    confirm(hooks, proposal, 'create-v2')
    rid = proposal['request_id']

    compact = call(human_tools, 'ancor_request_get', {'request_id': rid})
    assert compact['content']['exact_via']['tool'] == 'ancor_request_get'
    rejected_delivery = call(coordinator_tools, 'ancor_delivery_reject', {
        'request_id': rid, 'reason': 'No delivery exists yet'})
    assert rejected_delivery['status'] == 'active'
    state = call(human_tools, 'ancor_request_get', {
        'request_id': rid, 'include_content': True})
    versions = [point['version_id'] for points in state['content']['fields'].values()
                for point in points]
    invalid_runtime = json.loads(coordinator_tools['ancor_task_create']['handler']({
        'request_id': rid, 'key': 'bad-runtime', 'title': 'Invalid',
        'contract': contract(), 'generation': 1, 'point_versions': versions,
        'max_runtime_seconds': 0,
    }))
    assert invalid_runtime['code'] == 'invalid_request'
    missing_runtime = json.loads(coordinator_tools['ancor_task_create']['handler']({
        'request_id': rid, 'key': 'missing-runtime', 'title': 'Invalid',
        'contract': contract(), 'generation': 1, 'point_versions': versions,
    }))
    assert missing_runtime['code'] == 'invalid_request'
    assert board.calls == []
    task = call(coordinator_tools, 'ancor_task_create', {
        'request_id': rid, 'key': 'report-v2', 'title': 'Write report',
        'contract': contract(), 'generation': 1, 'point_versions': versions,
        'max_runtime_seconds': 3600,
    })
    assert 'Implement the requested result' in board.calls[0][1]['body']
    assert {key: value for key, value in board.calls[0][1].items() if key != 'body'} == {
        'title': 'Write report',
        'assignee': 'ancor-worker', 'parent_ids': [],
        'hold': True, 'max_runtime_seconds': 3600,
    }
    assert board.show(task['task_id'])['status'] == 'blocked'
    assert board.show(task['task_id'])['body'].startswith('ANCOR-LINK:')
    call(coordinator_tools, 'ancor_impact_review', {
        'request_id': rid, 'generation': 1,
        'decisions': [{'task_id': task['task_id'], 'decision': 'revise',
                       'reason': 'Bind the card to the current request'}],
        'coverage_reason': 'The task covers the current outcome and constraint',
    })
    call(coordinator_tools, 'ancor_task_contract_update', {
        'task_id': task['task_id'], 'contract': contract(outcome='Write verified report'),
        'generation': 1, 'point_versions': versions,
    })
    call(coordinator_tools, 'ancor_impact_finish', {
        'request_id': rid, 'generation': 1, 'task_id': task['task_id'],
        'evidence': 'Contract bound to the current request basis',
    })
    call(coordinator_tools, 'ancor_task_unblock', {
        'task_id': task['task_id'], 'reason': 'reviewed'})
    assert board.show(task['task_id'])['status'] == 'ready'
    history = call(human_tools, 'ancor_task_history', {
        'task_id': task['task_id'], 'kind': 'comments', 'limit': 100})
    assert history['items'] == []

    board.tasks[task['task_id']]['task']['status'] = 'done'
    evidence = {
        'generation': 1,
        'coverage': [{'version_id': version, 'satisfied': True,
                      'evidence': 'Verified against the delivered report'}
                     for version in versions],
        'verification': ['Checked the delivered report'],
        'artifacts': ['/tmp/report.md'],
        'task_ids': [task['task_id']],
    }
    verified = call(coordinator_tools, 'ancor_delivery_verify', {
        'request_id': rid, 'evidence': evidence})
    assert verified['status'] == 'awaiting_acceptance'
    overview = call(coordinator_tools, 'ancor_overview', {})
    waiting = next(item for item in overview['awaiting_acceptance']['items']
                   if item['id'] == rid)
    assert 'accept_via' not in waiting
    assert 'request_changes_via' not in waiting

    verified_state = call(coordinator_tools, 'ancor_request_get', {'request_id': rid})
    notice_id = verified_state['request']['completion_notice_id']
    notice = call(coordinator_tools, 'ancor_completion_send', {
        'id': notice_id, 'request_id': rid,
        'message': 'Verified report is ready for acceptance.'})
    assert notice['state'] == 'pending'

    accepted = call(human_tools, 'ancor_request_accept_propose', {'request_id': rid})
    confirm(hooks, accepted, 'accept-v2')
    assert Store(tmp_path / 'store.db').get_request(rid)['status'] == 'accepted'


def test_v2_admin_tool_is_opt_in_only(tmp_path, monkeypatch):
    _, normal, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-coordinator', mode='v2')
    assert 'ancor_admin_task_associate' not in normal
    _, admin, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-coordinator', mode='v2', admin=True)
    assert 'ancor_admin_task_associate' in admin


def test_legacy_and_dual_modes_are_explicit_rollback_paths(tmp_path, monkeypatch):
    _, legacy, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-coordinator', mode='legacy')
    assert 'ancor_board' in legacy
    assert 'ancor_task_create' not in legacy

    _, dual, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-coordinator', mode='dual')
    assert 'ancor_board' in dual
    assert 'ancor_task_create' in dual

    _, legacy_human, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-interactor', mode='legacy')
    _, dual_human, _, _ = registered(
        tmp_path, monkeypatch, 'ancor-interactor', mode='dual')
    old = call(legacy_human, 'ancor_signal', {'text': 'legacy path'})
    new = call(dual_human, 'ancor_signal_propose', {'text': 'v2 path'})
    assert old['id'] != new['id']


def test_host_uses_bounded_event_batch_configuration(tmp_path):
    store = Store(tmp_path / 'store.db')
    ctx = SimpleNamespace(
        profile_name='default',
        get_config=lambda key, default=None: {
            'store_path': store.path,
            'coordinator_profile': 'ancor-coordinator',
            'event_batch_size': 7,
        }.get(key, default),
    )
    host = Host(ctx, store=store, board=SimpleNamespace(list=list))
    assert host.coordinator.batch_size == 7

    invalid = SimpleNamespace(
        profile_name='default',
        get_config=lambda key, default=None: {
            'store_path': store.path,
            'coordinator_profile': 'ancor-coordinator',
            'event_batch_size': 33,
        }.get(key, default),
    )
    try:
        Host(invalid, store=store, board=SimpleNamespace(list=list))
    except ValueError as exc:
        assert 'event_batch_size' in str(exc)
    else:
        raise AssertionError('invalid event_batch_size was accepted')


def test_reference_rewrite_is_role_aware_and_preserves_unknown_business_data():
    unknown = {'tool': 'external', 'args': {'action': 'keep', 'value': 1}}
    assert rewrite_legacy_references(unknown, role='coordinator') == unknown
    interactor = rewrite_legacy_references({
        'accept_via': {'tool': 'ancor_request',
                       'args': {'action': 'accept', 'request_id': 'r1'}},
    }, role='interactor')
    assert interactor['accept_via'] == {
        'tool': 'ancor_request_accept_propose', 'args': {'request_id': 'r1'}}
    coordinator = rewrite_legacy_references({
        'accept_via': {'tool': 'ancor_request',
                       'args': {'action': 'accept', 'request_id': 'r1'}},
        'keep': unknown,
    }, role='coordinator')
    assert coordinator == {'keep': unknown}
