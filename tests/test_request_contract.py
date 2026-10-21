"""Action-level request validation, question policy and role-safe board access."""
import json
import sqlite3
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
    assert call(tools, 'ancor_board', {'action': 'show', 'task_id': 't1'})['status'] == 'blocked'
    first = call(tools, 'ancor_notify', args)
    assert first['id'] == 'q1'
    assert first['state'] == 'pending'

    duplicate = call(tools, 'ancor_notify', dict(args, id='q2'))
    assert duplicate['code'] == 'state_conflict'
    assert 'q1' in duplicate['error']

    tasks['t1']['status'] = 'running'
    retry = call(tools, 'ancor_notify', args)
    assert retry['id'] == 'q1'
    assert retry['state'] == 'pending'
    assert [row['id'] for row in NotificationOutbox.read_existing(
        store.path + '.notifications.db')] == ['q1']


def test_draft_requires_authoritative_title(tmp_path, monkeypatch):
    tools, _, _ = registered(tmp_path, monkeypatch, 'ancor-interactor', {})
    missing = call(tools, 'ancor_requirement', {
        'action': 'draft', 'brief': 'Brief', 'acceptance_criteria': 'Criteria'})
    assert missing['code'] == 'invalid_request'
    assert missing['missing_fields'] == ['title']
    prepared = call(tools, 'ancor_requirement', {
        'action': 'draft', 'title': 'Stable title',
        'brief': 'Brief', 'acceptance_criteria': 'Criteria'})
    assert prepared['title'] == 'Stable title'
    assert 'Title: Stable title' in prepared['clarify_args']['questions'][0]['question']


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


def test_coordinator_can_withdraw_superseded_question(tmp_path, monkeypatch):
    tasks = {'t1': {'id': 't1', 'status': 'blocked'}}
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-coordinator', tasks)
    store.link_task(req['id'], 't1')
    args = {'action': 'send', 'id': 'q1', 'kind': 'question', 'message': 'Which label?',
            'requirement_id': req['id'], 'task_id': 't1'}
    assert call(tools, 'ancor_notify', args)['state'] == 'pending'
    closed = call(tools, 'ancor_notify', {
        'action': 'withdraw', 'id': 'q1', 'reason': 'new input supplied the label'})
    assert closed['closed'] is True
    assert closed['close_reason'] == 'new input supplied the label'
    listed = call(tools, 'ancor_notify', {'action': 'list', 'open_only': True})
    assert listed['notifications']['count'] == 0


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
    assert [task['id'] for task in listed['items']] == ['t1']
    shown = call(tools, 'ancor_board', {'action': 'show', 'task_id': 't1'})
    assert shown['id'] == 't1'
    context = call(tools, 'ancor_get_context', {})
    assert [task['id'] for task in context['tasks']['items']] == ['t1']
    denied = call(tools, 'ancor_board', {'action': 'block', 'task_id': 't1', 'reason': 'No'})
    assert denied['code'] == 'forbidden'


def test_expanded_context_reports_paused_state_and_reason(tmp_path, monkeypatch):
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-interactor', {})
    pause = store.create_requirement_command('pause', req['id'], 'User asked', 'Stop briefly')
    store.confirm_requirement_command(pause['id'], 'pause-confirmation')
    context = call(tools, 'ancor_get_context', {'requirement_id': req['id']})
    assert context['requirement']['status'] == 'paused'
    assert context['progression'][0]['state'] == 'paused'
    assert context['transitions']['items'][0]['kind'] == 'requirement.paused'
    assert context['transitions']['items'][0]['reason_preview'] == 'User asked'


def test_notification_tool_output_does_not_expose_transport_error_key(tmp_path, monkeypatch):
    tasks = {'t1': {'id': 't1', 'status': 'blocked'}}
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-interactor', tasks)
    store.link_task(req['id'], 't1')
    from ancor.notification import NotificationOutbox
    box = NotificationOutbox(store.path + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    box.enqueue('q1', 'question', 'Need the exact input', actor='ancor-coordinator',
                requirement_id=req['id'], task_id='t1')
    db = sqlite3.connect(store.path + '.notifications.db')
    db.execute("UPDATE human_notifications SET error=? WHERE id='q1'",
               (json.dumps({'reason_code': 'transport_unavailable'}),))
    db.commit()
    db.close()
    listed = call(tools, 'ancor_notify', {'action': 'list', 'id': 'q1'})
    row = listed['notifications']['items'][0]
    assert row['delivery_error']
    assert 'error' not in row


def response_service(store):
    from ancor.service import Service
    config = {'store_path': store.path, 'coordinator_profile': 'ancor-coordinator',
              'human_target': 'test:destination'}
    ctx = SimpleNamespace(get_config=lambda key, default=None: config.get(key, default))
    return Service(ctx, store)


def submitted(args):
    question = args['questions'][0]
    return {'outcome': 'submitted', 'responses': [{
        'question': question['question'], 'choices_offered': question['choices'],
        'status': 'answered', 'user_response': question['choices'][1]}]}


def queued_question(store, req):
    store.link_task(req['id'], 't1')
    box = NotificationOutbox(store.path + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    box.enqueue('q1', 'question', 'Which label?', actor='ancor-coordinator',
                requirement_id=req['id'], task_id='t1')
    return box


def test_question_response_is_recorded_only_after_exact_native_confirmation(tmp_path, monkeypatch):
    tasks = {'t1': {'id': 't1', 'status': 'blocked'}}
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-interactor', tasks)
    box = queued_question(store, req)

    proposal = call(tools, 'ancor_notify', {
        'action': 'respond', 'id': 'q1', 'answer': 'Quarterly'})
    args = proposal['clarify_args']
    assert 'Exact human reply to be recorded:\nQuarterly' in args['questions'][0]['question']
    assert box.get('q1')['answer'] is None, 'the reply must not be durable before confirmation'

    service = response_service(store)
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    service.post_tool_call('clarify', args, submitted(args), session_id='s', tool_call_id='c')

    assert box.get('q1')['answer'] == 'Quarterly'
    call(tools, 'ancor_notify', {'action': 'list', 'id': 'q1'})
    events = [event for event in store.snapshot()['pending_events']
              if event['kind'] == 'notification.response']
    assert len(events) == 1
    assert events[0]['payload']['body'] == 'Quarterly'
    assert events[0]['payload']['confirmed'] is True
    assert events[0]['payload']['source'] == 'native_clarify_response'
    assert events[0]['payload']['authorization'] is False


def test_question_response_confirmation_rejects_cancel_and_stale_reply(tmp_path, monkeypatch):
    tasks = {'t1': {'id': 't1', 'status': 'blocked'}}
    tools, store, req = registered(tmp_path, monkeypatch, 'ancor-interactor', tasks)
    box = queued_question(store, req)
    service = response_service(store)

    first = call(tools, 'ancor_notify', {
        'action': 'respond', 'id': 'q1', 'answer': 'Quarterly'})['clarify_args']
    second = call(tools, 'ancor_notify', {
        'action': 'respond', 'id': 'q1', 'answer': 'Monthly'})['clarify_args']
    assert service.pre_tool_call('clarify', first, session_id='s1', tool_call_id='c1')['action'] == 'block'
    assert box.get('q1')['answer'] is None

    assert service.pre_tool_call('clarify', second, session_id='s2', tool_call_id='c2') is None
    cancelled = submitted(second)
    cancelled['outcome'] = 'cancelled'
    service.post_tool_call('clarify', second, cancelled, session_id='s2', tool_call_id='c2')
    assert box.get('q1')['answer'] is None

    assert service.pre_tool_call('clarify', second, session_id='s3', tool_call_id='c3') is None
    service.post_tool_call('clarify', second, submitted(second), session_id='s3', tool_call_id='c3')
    assert box.get('q1')['answer'] == 'Monthly'
