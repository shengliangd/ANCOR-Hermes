import json
from types import SimpleNamespace

import ancor
from ancor.host import Host
from ancor.notification import NotificationOutbox
from ancor.scheduling import Scheduling
from ancor.store import Store


class Context:
    profile_name = 'default'

    def __init__(self, path):
        self.config = {'store_path': str(path), 'board': 'ancor',
                       'coordinator_profile': 'ancor-coordinator'}

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_wake_packet_excludes_full_briefs_and_bodies_under_incident_fixture(tmp_path):
    store = Store(tmp_path / 'store.db')
    tasks = []
    for index in range(3):
        request = store.confirm(store.create_draft(
            'FULL BRIEF ' * 3000,
            title=f'Incident request {index}')['id'], f'human:{index}')
        task_id = f'task-{index}'
        store.link_task(request['id'], task_id)
        tasks.append({'id': task_id, 'title': f'Incident task {index}',
                      'status': 'running', 'assignee': 'ancor-worker',
                      'body': 'FULL TASK BODY ' * 3000,
                      'blocked_reason': 'FULL REASON ' * 2000})
    scheduler = Scheduling(store)
    for request in store.snapshot()['requests']:
        scheduler.call({'action': 'schedule', 'request_id': request['id'],
                        'reason': 'FULL WAKE REASON ' * 2000,
                        'due_at': 4102444800})
    box = NotificationOutbox(str(tmp_path / 'store.db') + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    for index in range(12):
        box.enqueue(f'n{index}', 'completion', f'FULL MESSAGE {index} ' * 2000,
                    actor='ancor-coordinator',
                    request_id=store.snapshot()['requests'][index % 3]['id'])
    store.add_event('native.completed', {'result': 'FULL EVENT ' * 5000})

    host = Host(Context(tmp_path / 'store.db'), store=store,
                board=SimpleNamespace(), db_path=tmp_path / 'native.db')
    host.tasks = tasks
    packet = host.wake_packet()
    encoded = json.dumps(packet, ensure_ascii=False)
    assert len(encoded) < 8192
    assert 'FULL BRIEF' not in encoded
    assert 'FULL CRITERIA' not in encoded
    assert 'FULL TASK BODY' not in encoded
    assert ('FULL MESSAGE ' * 100) not in encoded
    assert 'acceptances' not in packet


def test_default_overview_is_bounded_under_history_heavy_fixture(tmp_path, monkeypatch):
    tools = {}
    config = {'store_path': str(tmp_path / 'store.db'), 'board': 'ancor',
              'coordinator_profile': 'ancor-coordinator',
              'human_target': 'test:destination', 'transport_profile': 'default'}
    ctx = SimpleNamespace(
        profile_name='ancor-coordinator',
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **kw: tools.update({kw['name']: kw}),
        register_hook=lambda *args: None,
        register_platform_handler=lambda *args: None)
    store = Store(config['store_path'])
    request = store.confirm(store.create_draft(
        'BRIEF ' * 2000, title='Bounded overview')['id'],
        'human:overview')
    tasks = []
    for index in range(90):
        status = 'done' if index < 70 else 'running'
        task = {'id': f't{index}', 'title': f'Task {index}', 'status': status,
                'assignee': 'ancor-worker', 'created_at': index}
        if index >= 80:
            task['archived_at'] = 100
            task['status'] = 'done'
        store.link_task(request['id'], task['id'])
        tasks.append(task)
    box = NotificationOutbox(str(store.path) + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    for index in range(20):
        box.enqueue(f'n{index}', 'question', 'Message ' * 200,
                    actor='ancor-coordinator',
                    request_id=request['id'], task_id=f'question-task-{index}')
        if index < 14:
            box.close_questions({f'n{index}': 'fixture history'}, closed_by='host')
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: tasks)
    ancor.register(ctx)

    result = json.loads(tools['ancor_get_context']['handler']({}))
    encoded = json.dumps(result, ensure_ascii=False)
    assert len(encoded) < 10240, len(encoded)
    assert result['tasks']['count'] == 10
    assert result['counts']['tasks']['total'] == 90
    assert result['counts']['tasks']['terminal'] == 80
    assert result['notifications']['count'] == 6
    assert result['counts']['notifications']['closed'] == 14
    assert {item['kind'] for item in result['omitted']} == {
        'tasks', 'notifications'}
    assert all('count' not in item for item in result['omitted'])
    assert result['counts']['tasks']['by_status']['done'] == 70
    assert result['counts']['tasks']['by_status']['archived'] == 10


def test_request_expansion_scopes_every_nested_collection(tmp_path, monkeypatch):
    tools = {}
    store_path = tmp_path / 'store.db'
    config = {'store_path': str(store_path), 'board': 'ancor',
              'coordinator_profile': 'ancor-coordinator',
              'human_target': 'test:destination', 'transport_profile': 'default'}
    ctx = SimpleNamespace(
        profile_name='ancor-coordinator',
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **kw: tools.update({kw['name']: kw}),
        register_hook=lambda *args: None,
        register_platform_handler=lambda *args: None)
    store = Store(store_path)
    first = store.confirm(store.create_draft('first', title='First')['id'], 'human:1')
    second = store.confirm(store.create_draft('second', title='Second')['id'], 'human:2')
    store.link_task(first['id'], 'first-task')
    store.link_task(second['id'], 'second-task')
    store.ack_events([event['id'] for event in store.pending_events()])
    for index in range(25):
        store.add_event('request.paused', {
            'request_id': first['id'], 'command_id': f'first-{index}',
            'reason': f'first reason {index}'})
    store.add_event('request.paused', {
        'request_id': second['id'], 'command_id': 'second', 'reason': 'second'})
    store.ack_events([event['id'] for event in store.pending_events()])
    first_event = store.add_event(
        'native.completed', {'request_id': first['id'], 'task_id': 'first-task'})
    store.add_event(
        'native.completed', {'request_id': second['id'], 'task_id': 'second-task'})
    scheduler = Scheduling(store)
    for request, label in ((first, 'first'), (second, 'second')):
        scheduler.call({'action': 'schedule', 'request_id': request['id'],
                        'reason': label, 'due_at': 4102444800})
    box = NotificationOutbox(str(store_path) + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    box.enqueue('first-question', 'question', 'first', actor='ancor-coordinator',
                request_id=first['id'], task_id='first-task')
    box.enqueue('second-question', 'question', 'second', actor='ancor-coordinator',
                request_id=second['id'], task_id='second-task')
    tasks = [
        {'id': 'first-task', 'title': 'First task', 'status': 'running'},
        {'id': 'second-task', 'title': 'Second task', 'status': 'running'},
    ]
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: tasks)
    ancor.register(ctx)

    result = json.loads(tools['ancor_get_context']['handler'](
        {'request_id': first['id']}))
    assert result['request']['id'] == first['id']
    assert [task['id'] for task in result['tasks']['items']] == ['first-task']
    assert [row['id'] for row in result['notifications']['items']] == ['first-question']
    assert [wake['request_id'] for wake in result['scheduled_wakes']['items']] == [first['id']]
    assert [item['request_id'] for item in result['progression']] == [first['id']]
    assert [event['id'] for event in result['pending_events']['items']] == [first_event['id']]
    assert result['transitions']['count'] == 26
    assert 'items' not in result['transitions']
    history = json.loads(tools['ancor_get_context']['handler'](
        result['transitions']['exact_via']['args']))
    assert history['transitions']['returned'] == 20
    assert history['transitions']['truncated'] is True
    assert {item['request_id'] for item in history['transitions']['items']} == {first['id']}
    older = json.loads(tools['ancor_get_context']['handler'](
        history['transitions']['next']['args']))
    assert older['transitions']['returned'] == 6
    assert older['transitions']['truncated'] is False
    ids = [item['id'] for page in [history, older] for item in page['transitions']['items']]
    assert len(set(ids)) == 26


def test_scheduled_wake_observation_is_bounded(tmp_path):
    store = Store(tmp_path / 'store.db')
    request = store.confirm(store.create_draft('wakes', title='Wakes')['id'],
                                'human:wake')
    scheduler = Scheduling(store)
    for index in range(55):
        scheduler.call({'action': 'schedule', 'request_id': request['id'],
                        'reason': f'wake {index}', 'due_at': 4102444800 + index})

    observation = scheduler.observation(store.snapshot())
    wakes = observation['scheduled_wakes']
    assert wakes['count'] == 55
    assert wakes['returned'] == 50
    assert wakes['truncated'] is True


def test_interactor_draft_collection_is_bounded(tmp_path, monkeypatch):
    tools = {}
    config = {'store_path': str(tmp_path / 'store.db'), 'board': 'ancor',
              'interactor_profile': 'ancor-interactor',
              'coordinator_profile': 'ancor-coordinator',
              'human_target': 'test:destination', 'transport_profile': 'default'}
    ctx = SimpleNamespace(
        profile_name='ancor-interactor',
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **kw: tools.update({kw['name']: kw}),
        register_hook=lambda *args: None,
        register_platform_handler=lambda *args: None)
    store = Store(config['store_path'])
    for index in range(25):
        store.create_draft('brief ' * 200,
                           summary='summary ' * 200, title=f'Draft {index}')
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: [])
    ancor.register(ctx)

    result = json.loads(tools['ancor_get_context']['handler']({}))
    assert result['drafts']['count'] == 25
    assert result['drafts']['returned'] == 20
    assert result['drafts']['truncated'] is True
    assert 'summary' not in result['drafts']['items'][0]
    assert result['drafts']['items'][0]['summary_preview'].startswith('summary')
