"""Action-level request validation, question policy and role-safe board access."""
import json
from types import SimpleNamespace

import ancor
from ancor.notification import NotificationOutbox
from ancor.store import Store


def registered(tmp_path, monkeypatch, profile, tasks):
    tools = {}
    config = {'store_path': str(tmp_path / 'store.db'), 'board': 'ancor',
              'coordinator_profile': 'ancor-coordinator',
              'interactor_profile': 'ancor-interactor',
              'human_target': 'test:destination', 'transport_profile': 'default'}
    ctx = SimpleNamespace(
        profile_name=profile,
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **kw: tools.update({kw['name']: kw}),
        register_hook=lambda *args: None,
        register_platform_handler=lambda *args: None)
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: list(tasks.values()))
    monkeypatch.setattr('ancor.board.NativeBoard.show', lambda self, task_id: tasks[task_id])
    monkeypatch.setattr('ancor.board.NativeBoard._call', lambda self, *args, **kwargs: {
        'task': tasks[args[1]], 'comments': [], 'runs': [], 'events': []})
    monkeypatch.setattr('ancor.notification.HermesSender.__call__',
                        lambda self, target, message: {'success': True})
    ancor.register(ctx)
    store = Store(config['store_path'])
    req = store.confirm(store.create_draft('Deliver report', 'Exact totals')['id'], 'fixture')
    store.ack_events([event['id'] for event in store.pending_events()])
    return tools, store, req


def call(tools, name, args, **kwargs):
    return json.loads(tools[name]['handler'](args, **kwargs))


def test_question_requires_blocked_task_and_reuses_one_open_question(tmp_path, monkeypatch):
    tasks = {'t1': {'id': 't1', 'status': 'running'}}
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-coordinator', tasks)
    store.link_task(req['id'], 't1')
    args = {'action': 'send', 'id': 'q1', 'kind': 'question', 'message': 'Which label?',
            'requirement_id': req['id'], 'task_id': 't1'}

    running = call(tools, 'ancor_notify', args)
    assert running['code'] == 'state_conflict'
    assert 'blocked' in running['error'] and 'running' in running['error']

    tasks['t1']['status'] = 'blocked'
    assert call(tools, 'ancor_board', {'action': 'show', 'task_id': 't1'})['task']['status'] == 'blocked'
    first = call(tools, 'ancor_notify', args)
    assert first['id'] == 'q1'
    assert first['state'] == 'sent'

    duplicate = call(tools, 'ancor_notify', dict(args, id='q2'))
    assert duplicate['code'] == 'state_conflict'
    assert 'q1' in duplicate['error']

    tasks['t1']['status'] = 'running'
    retry = call(tools, 'ancor_notify', args)
    assert retry['id'] == 'q1'
    assert [row['id'] for row in NotificationOutbox.read_existing(
        store.path + '.notifications.db')] == ['q1']


def test_open_question_scope_is_requirement_and_task(tmp_path, monkeypatch):
    tasks = {'t1': {'id': 't1', 'status': 'blocked'}}
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-coordinator', tasks)
    other = store.confirm(store.create_draft('Other report', 'Other totals')['id'], 'fixture')
    store.link_task(req['id'], 't1')
    store.link_task(other['id'], 't1')
    base = {'action': 'send', 'kind': 'question', 'message': 'Which label?', 'task_id': 't1'}
    assert not call(tools, 'ancor_notify',
                    dict(base, id='q1', requirement_id=req['id'])).get('error')
    assert not call(tools, 'ancor_notify',
                    dict(base, id='q2', requirement_id=other['id'])).get('error')


def test_requirement_response_is_idempotent(tmp_path, monkeypatch):
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-interactor', {})
    args = {'action': 'respond', 'requirement_id': req['id'], 'body': 'Use quarterly'}
    first = call(tools, 'ancor_requirement', args, session_id='s', tool_call_id='c')
    second = call(tools, 'ancor_requirement', args, session_id='s', tool_call_id='c')
    assert first['id'] == second['id']
    responses = [event for event in store.snapshot()['pending_events']
                 if event['kind'] == 'requirement.response']
    assert len(responses) == 1


def test_interactor_pause_action_requires_reason_and_only_proposes(tmp_path, monkeypatch):
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-interactor', {})
    missing = call(tools, 'ancor_requirement', {
        'action': 'pause', 'requirement_id': req['id'], 'summary': 'Stop briefly'})
    assert missing['code'] == 'invalid_request'
    assert missing['missing_fields'] == ['reason']
    proposal = call(tools, 'ancor_requirement', {
        'action': 'pause', 'requirement_id': req['id'],
        'summary': 'Stop briefly', 'reason': 'User asked'})
    assert proposal['action'] == 'pause'
    assert 'ANCOR pause ' + proposal['id'] in proposal['clarify_args']['questions'][0]['question']
    assert store.get_requirement(req['id'])['status'] == 'active'


def test_missing_action_fields_return_actionable_validation(tmp_path, monkeypatch):
    tasks = {'t1': {'id': 't1', 'status': 'blocked'}}
    coordinator, _, req = registered(tmp_path, monkeypatch, 'ancor-coordinator', tasks)
    result = call(coordinator, 'ancor_board', {'action': 'create', 'key': 'k', 'title': 't',
                                                'body': 'b', 'contract': {}})
    assert result['code'] == 'invalid_request'
    assert result['missing_fields'] == ['requirement_id', 'contract']
    assert 'next_step' in result

    interactor, _, _ = registered(tmp_path, monkeypatch, 'ancor-interactor', tasks)
    result = call(interactor, 'ancor_requirement', {'action': 'confirm'})
    assert result['code'] == 'invalid_request'
    assert result['missing_fields'] == ['draft_id']


def test_interactor_can_read_but_not_mutate_board(tmp_path, monkeypatch):
    tasks = {'t1': {'id': 't1', 'title': 'Report', 'status': 'blocked'}}
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-interactor', tasks)
    store.link_task(req['id'], 't1')
    listed = call(tools, 'ancor_board', {'action': 'list'})
    assert [task['id'] for task in listed['tasks']] == ['t1']
    shown = call(tools, 'ancor_board', {'action': 'show', 'task_id': 't1'})
    assert shown['task']['id'] == 't1'
    context = call(tools, 'ancor_get_context', {})
    assert [task['id'] for task in context['tasks']] == ['t1']
    denied = call(tools, 'ancor_board', {'action': 'block', 'task_id': 't1', 'reason': 'No'})
    assert denied['code'] == 'forbidden'


def test_expanded_context_reports_paused_state_and_reason(tmp_path, monkeypatch):
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-interactor', {})
    pause = store.create_requirement_command('pause', req['id'], 'User asked', 'Stop briefly')
    store.confirm_requirement_command(pause['id'], 'pause-confirmation')
    context = call(tools, 'ancor_get_context', {'requirement_id': req['id']})
    assert context['requirement']['status'] == 'paused'
    assert context['progression'][0]['state'] == 'paused'
    assert context['transitions'][0]['kind'] == 'requirement.paused'
    assert context['transitions'][0]['reason'] == 'User asked'
