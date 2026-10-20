"""Compact defaults and explicit expansion for model-facing tool output."""
import json
from types import SimpleNamespace

import ancor
from ancor.notification import NotificationOutbox
from ancor.operations import Operations
from ancor.store import Store


def task(body='FULL CONTRACT'):
    return {'id': 't1', 'title': 'Bounded task', 'status': 'running',
            'assignee': 'ancor-worker', 'created_at': 1, 'last_heartbeat_at': 2,
            'body': body}


def test_board_list_is_compact_and_show_expands(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = store.confirm(store.create_draft('Brief', 'Criteria')['id'], 'fixture')
    store.link_task(req['id'], 't1')
    card = task('FULL CONTRACT ' * 100)

    class Board:
        def list(self):
            return [card]

        def _call(self, *args, **kwargs):
            return {'task': card, 'comments': [], 'runs': [], 'events': []}

    ctx = SimpleNamespace(profile_name='ancor-coordinator',
                          get_config=lambda key, default=None:
                          {'coordinator_profile': 'ancor-coordinator'}.get(key, default))
    operations = Operations(ctx, store, Board())
    listed = operations.call({'action': 'list'})
    assert listed['tasks'][0]['id'] == 't1'
    assert listed['tasks'][0]['requirement_id'] == req['id']
    assert 'body' not in listed['tasks'][0]
    shown = operations.call({'action': 'show', 'task_id': 't1'})
    assert shown['task']['body'].startswith('FULL CONTRACT')


def test_context_is_compact_and_requirement_expands(tmp_path, monkeypatch):
    tools = {}
    store_path = tmp_path / 'store.db'
    config = {'store_path': str(store_path), 'board': 'ancor',
              'coordinator_profile': 'ancor-coordinator',
              'human_target': 'test:destination', 'transport_profile': 'default'}
    ctx = SimpleNamespace(profile_name='ancor-coordinator',
                          get_config=lambda key, default=None: config.get(key, default),
                          register_tool=lambda **kw: tools.update({kw['name']: kw}),
                          register_hook=lambda *args: None,
                          register_platform_handler=lambda *args: None)
    store = Store(store_path)
    brief = 'B' * 2000
    criteria = 'C' * 2000
    req = store.confirm(store.create_draft(brief, criteria, summary='Short summary')['id'], 'fixture')
    store.link_task(req['id'], 't1')
    card = task('FULL CONTRACT ' * 100)
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: [card])
    ancor.register(ctx)
    call = lambda args: json.loads(tools['ancor_get_context']['handler'](args))

    overview = call({})
    requirement = overview['requirements'][0]
    assert requirement['id'] == req['id']
    assert requirement['summary'] == 'Short summary'
    assert len(requirement['brief_preview']) < len(brief)
    assert requirement['truncated'] is True
    assert 'brief' not in requirement
    assert requirement['brief_truncated'] is True
    assert requirement['acceptance_criteria_truncated'] is True
    assert 'body' not in overview['tasks'][0]
    event = overview['pending_events'][0]
    assert 'payload' not in event and event['payload_preview']

    full_event = call({'event_id': event['id']})
    assert full_event['event']['payload']['requirement_id'] == req['id']

    expanded = call({'requirement_id': req['id']})
    assert expanded['requirement']['brief'] == brief
    assert expanded['requirement']['acceptance_criteria'] == criteria
    assert expanded['tasks'][0]['id'] == 't1'
    assert 'body' not in expanded['tasks'][0]


def test_notification_list_is_compact_and_id_expands(tmp_path):
    tools = {}
    store_path = tmp_path / 'store.db'
    config = {'store_path': str(store_path), 'board': 'ancor',
              'coordinator_profile': 'ancor-coordinator',
              'human_target': 'test:destination', 'transport_profile': 'default'}
    ctx = SimpleNamespace(profile_name='ancor-coordinator',
                          get_config=lambda key, default=None: config.get(key, default),
                          register_tool=lambda **kw: tools.update({kw['name']: kw}),
                          register_hook=lambda *args: None,
                          register_platform_handler=lambda *args: None)
    store = Store(store_path)
    req = store.confirm(store.create_draft('Brief', 'Criteria')['id'], 'fixture')
    store.link_task(req['id'], 't1')
    box = NotificationOutbox(store.path + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination',
                             sender=lambda target, message: {'success': True})
    message = 'Question ' + 'Q' * 1000
    box.enqueue('q1', 'question', message, actor='ancor-coordinator',
                requirement_id=req['id'], task_id='t1')
    box.deliver('q1', actor='ancor-coordinator')
    ancor.register(ctx)
    call = lambda args: json.loads(tools['ancor_notify']['handler'](args))

    overview = call({'action': 'list'})
    row = overview['notifications'][0]
    assert row['id'] == 'q1' and row['answered'] is False
    assert 'message' not in row and row['message_truncated'] is True
    assert call({'action': 'list', 'open_only': True})['count'] == 1
    assert call({'action': 'list', 'requirement_id': req['id']})['count'] == 1
    assert call({'action': 'list', 'task_id': 't1'})['count'] == 1
    assert call({'action': 'list', 'requirement_id': 'other'})['count'] == 0
    full = call({'action': 'list', 'id': 'q1'})['notifications'][0]
    assert full['message'] == message


def test_tool_errors_expose_a_stable_code(tmp_path):
    tools = {}
    config = {'store_path': str(tmp_path / 'store.db'), 'board': 'ancor',
              'coordinator_profile': 'ancor-coordinator',
              'human_target': 'test:destination', 'transport_profile': 'default'}
    ctx = SimpleNamespace(profile_name='ancor-coordinator',
                          get_config=lambda key, default=None: config.get(key, default),
                          register_tool=lambda **kw: tools.update({kw['name']: kw}),
                          register_hook=lambda *args: None,
                          register_platform_handler=lambda *args: None)
    ancor.register(ctx)
    result = json.loads(tools['ancor_board']['handler'](
        {'action': 'show', 'task_id': 'missing'}))
    assert result['code'] == 'invalid_request'


def test_board_list_prioritizes_live_recent_tasks_before_truncation(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = store.confirm(store.create_draft('Brief', 'Criteria')['id'], 'fixture')
    cards = [
        {'id': 'old-done', 'title': 'old', 'status': 'done', 'created_at': 1},
        {'id': 'old-running', 'title': 'running', 'status': 'running', 'created_at': 2},
        {'id': 'new-running', 'title': 'new', 'status': 'running', 'created_at': 3},
        {'id': 'archived', 'title': 'archived', 'status': 'done',
         'created_at': 10, 'archived_at': 20},
    ]
    for card in cards:
        store.link_task(req['id'], card['id'])

    class Board:
        def list(self):
            return cards

    ctx = SimpleNamespace(profile_name='ancor-coordinator',
                          get_config=lambda key, default=None:
                          {'coordinator_profile': 'ancor-coordinator'}.get(key, default))
    operations = Operations(ctx, store, Board())
    listed = operations.call({'action': 'list', 'limit': 2})
    assert [task['id'] for task in listed['tasks']] == ['new-running', 'old-running']
    assert listed['truncated'] is True
    with_archived = operations.call({'action': 'list', 'include_archived': True})
    assert with_archived['tasks'][-1]['id'] == 'archived'


def test_requirement_expansion_hides_archived_tasks(tmp_path, monkeypatch):
    tools = {}
    store_path = tmp_path / 'store.db'
    config = {'store_path': str(store_path), 'board': 'ancor',
              'coordinator_profile': 'ancor-coordinator',
              'human_target': 'test:destination', 'transport_profile': 'default'}
    ctx = SimpleNamespace(profile_name='ancor-coordinator',
                          get_config=lambda key, default=None: config.get(key, default),
                          register_tool=lambda **kw: tools.update({kw['name']: kw}),
                          register_hook=lambda *args: None,
                          register_platform_handler=lambda *args: None)
    store = Store(store_path)
    req = store.confirm(store.create_draft('Brief', 'Criteria')['id'], 'fixture')
    store.link_task(req['id'], 'live')
    store.link_task(req['id'], 'old')
    cards = [
        {'id': 'live', 'title': 'live', 'status': 'running', 'created_at': 1},
        {'id': 'old', 'title': 'old', 'status': 'done', 'created_at': 2,
         'archived_at': 3},
    ]
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: cards)
    ancor.register(ctx)
    expanded = json.loads(tools['ancor_get_context']['handler'](
        {'requirement_id': req['id']}))
    assert [task['id'] for task in expanded['tasks']] == ['live']
    assert expanded['task_count'] == 1
    assert expanded['archived_task_count'] == 1


def test_event_summaries_preview_payload_and_keep_references():
    events = [{'id': 1, 'kind': 'native.completed',
               'payload': {'task_id': 't1', 'result': 'R' * 2000}}]
    summary = ancor.event_summaries(events, 100)[0]
    assert summary['id'] == 1 and summary['task_id'] == 't1'
    assert 'payload' not in summary
    assert summary['payload_truncated'] is True
    assert len(summary['payload_preview']) < 700


def test_context_notification_selection_keeps_open_questions_visible():
    rows = [{'id': 'q-old', 'kind': 'question', 'answer': None}]
    rows += [{'id': f'c{i}', 'kind': 'completion', 'answer': None}
             for i in range(25)]
    selected = ancor.notification_selection(rows, 20)
    assert len(selected) == 20
    assert selected[0]['id'] == 'q-old'
    assert selected[-1]['id'] == 'c24'
