import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ancor.notification import NotificationOutbox
from ancor.store import Store


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    for key in ('ANCOR_STORE_PATH', 'ANCOR_DB_PATH', 'ANCOR_NOTIFICATIONS_PATH',
                'HERMES_KANBAN_BOARD'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    # Standalone use exercises the fallback without borrowing a real instance.
    monkeypatch.setitem(sys.modules, 'hermes_constants', None)
    return importlib.import_module('ancor.dashboard.plugin_api')


def client_for(api):
    app = FastAPI()
    app.include_router(api.router, prefix='/api/plugins/ancor')
    return TestClient(app)


def write_settings(home, **settings):
    home.mkdir(parents=True, exist_ok=True)
    # JSON is valid YAML and keeps this fixture independent of YAML serializers.
    (home / 'config.yaml').write_text(json.dumps({
        'plugins': {'entries': {'ancor': {'settings': settings}}}}))


def test_read_only_structured_detail_and_audit_redaction(tmp_path, monkeypatch):
    path = tmp_path / 'requests.db'
    store = Store(path)
    proposal = store.propose_change(None, [{'op': 'create', 'title': 'Report', 'fields': {'outcome': ['Deliver report']}}],
                                    actor='interactor', source={'input': 'Deliver report', 'token': 'secret-source'})
    rid = store.commit_change(proposal['id'], 'private-confirmation')['request_id']
    monkeypatch.setenv('ANCOR_STORE_PATH', str(path))
    api = importlib.import_module('ancor.dashboard.plugin_api')
    app = FastAPI()
    app.include_router(api.router, prefix='/api/plugins/ancor')
    before = path.read_bytes()
    with TestClient(app) as client:
        result = client.get('/api/plugins/ancor/requests/' + rid)
        assert result.status_code == 200
        detail = result.json()
        assert detail['fields']['outcome'][0]['body'] == 'Deliver report'
        assert detail['changes']['count'] == 1
        audit = client.get('/api/plugins/ancor/changes/' + proposal['id']).json()
        assert audit['confirmation_reference'] == '[redacted]'
        assert audit['source']['token'] == '[redacted]'
        assert client.post('/api/plugins/ancor/requests/' + rid, json={}).status_code == 405
    assert path.read_bytes() == before


def test_installed_dashboard_import_is_isolated_from_repository(tmp_path):
    source = Path(__file__).parents[1] / 'ancor'
    installs = []
    for name in ('first', 'second'):
        package = tmp_path / name / 'plugins' / 'ancor'
        shutil.copytree(source, package, ignore=shutil.ignore_patterns('__pycache__'))
        # Loading a dashboard must not register the agent plugin as a side effect.
        (package / '__init__.py').write_text('raise AssertionError("agent entrypoint imported")\n')
        installs.append(str(package / 'dashboard' / 'plugin_api.py'))
    script = '''
import importlib.util, sys
before = list(sys.path)
apis = []
for index, path in enumerate(sys.argv[1:]):
    name = 'hermes_dashboard_plugin_ancor_' + str(index)
    spec = importlib.util.spec_from_file_location(name, path)
    api = importlib.util.module_from_spec(spec)
    sys.modules[name] = api
    spec.loader.exec_module(api)
    assert api.health()['readonly'] is True
    apis.append(api)
assert sys.path == before
assert 'ancor' not in sys.modules
assert apis[0].request_ref is not apis[1].request_ref
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script, *installs],
                            cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_dashboard_uses_configured_store_and_board(tmp_path, monkeypatch, dashboard):
    home = Path(os.environ['HERMES_HOME'])
    path = tmp_path / 'custom.db'
    write_settings(home, store_path=str(path), board='custom-board',
                   coordinator_profile='custom-coordinator')
    store = Store(path)
    proposal = store.propose_change(None, [
        {'op': 'create', 'title': 'Configured request', 'fields': {'outcome': ['Deliver']}}],
        actor='interactor', source={'input': 'Deliver'})
    rid = store.commit_change(proposal['id'], 'fixture')['request_id']
    assert dashboard._notifications_path() == Path(str(path) + '.notifications.db')
    calls = []
    task = {'id': 'task', 'title': 'Configured task', 'status': 'done', 'body': 'Result'}

    def record(board, action):
        calls.append((board.prefix, board.env['HERMES_HOME'], action))

    def tasks(board):
        record(board, 'list')
        return [task]

    def show(board, *args):
        record(board, args)
        return {'task': task, 'comments': [], 'events': [], 'runs': []}

    monkeypatch.setattr('ancor.board.NativeBoard.list', tasks)
    monkeypatch.setattr('ancor.board.NativeBoard._call', show)
    before = path.read_bytes()
    with client_for(dashboard) as client:
        overview = client.get('/api/plugins/ancor/overview').json()
        assert overview['requests']['items'][0]['id'] == rid
        assert client.get('/api/plugins/ancor/tasks/task').status_code == 200
    assert calls and all(prefix == ['hermes', '-p', 'custom-coordinator', 'kanban',
                                    '--board', 'custom-board'] and root == str(home)
                         for prefix, root, _ in calls)
    assert ('show', 'task') in [action for _, _, action in calls]
    assert path.read_bytes() == before


def test_dashboard_follows_runtime_profile_and_explicit_overrides(tmp_path, monkeypatch, dashboard):
    homes = [tmp_path / 'profile-a', tmp_path / 'profile-b']
    for index, home in enumerate(homes):
        write_settings(home, store_path=str(tmp_path / f'store-{index}.db'))
    current = [homes[0]]
    monkeypatch.setitem(sys.modules, 'hermes_constants',
                        SimpleNamespace(get_hermes_home=lambda: current[0]))
    assert dashboard._store_path() == tmp_path / 'store-0.db'
    current[0] = homes[1]
    assert dashboard._store_path() == tmp_path / 'store-1.db'
    monkeypatch.setenv('ANCOR_DB_PATH', str(tmp_path / 'db-override'))
    assert dashboard._store_path() == tmp_path / 'db-override'
    monkeypatch.setenv('ANCOR_STORE_PATH', str(tmp_path / 'store-override'))
    assert dashboard._store_path() == tmp_path / 'store-override'
    monkeypatch.setenv('ANCOR_NOTIFICATIONS_PATH', str(tmp_path / 'notifications'))
    assert dashboard._notifications_path() == tmp_path / 'notifications'
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'board-override')
    assert dashboard._board().board == 'board-override'


def test_missing_instance_is_read_only(tmp_path, monkeypatch, dashboard):
    home = Path(os.environ['HERMES_HOME'])
    assert dashboard._store_path() == home / 'ancor' / 'requests.db'
    monkeypatch.setattr(dashboard, '_read_tasks', lambda: [])
    with client_for(dashboard) as client:
        assert client.get('/api/plugins/ancor/overview').json()['requests']['count'] == 0
    assert not home.exists()


def test_confirmation_metadata_is_private_on_all_dashboard_surfaces(tmp_path, monkeypatch, dashboard):
    path = tmp_path / 'requests.db'
    monkeypatch.setenv('ANCOR_STORE_PATH', str(path))
    monkeypatch.setattr(dashboard, '_read_tasks', lambda: [])
    store = Store(path)
    box = NotificationOutbox(str(path) + '.notifications.db',
                             coordinator_profile='coordinator', target='test:private-destination')
    box.enqueue('question', 'question', 'Which format?', actor='coordinator', request_id='request')
    box.claim_next()
    box.mark_sent('question', {'success': True, 'private_receipt': 'fixture-receipt'})
    proposal = box.propose_response('question', 'Markdown')
    reference = json.dumps({'source': 'native_clarify_response', 'session_id': 'private-session',
                            'tool_call_id': 'private-call', 'target': 'private-target'})
    box.confirm_response(proposal['id'], reference)
    event = store.add_event('notification.response', {
        'notification_id': 'question', 'body': 'Markdown', 'reference': reference,
        'nested': [{'SESSION_ID': 'private-session', 'tool_call_id': 'private-call'}]})
    # A future internal column must not silently become a browser-visible field.
    with sqlite3.connect(box.path) as db:
        db.execute('ALTER TABLE human_notifications ADD COLUMN internal_metadata TEXT')
        db.execute("UPDATE human_notifications SET internal_metadata='private-internal'")
    before = {p: p.read_bytes() for p in (path, Path(box.path))}
    with client_for(dashboard) as client:
        detail = client.get('/api/plugins/ancor/notifications/question').json()['notification']
        assert detail['message'] == 'Which format?' and detail['answer'] == 'Markdown'
        assert not {'answer_reference', 'target', 'receipt', 'internal_metadata'} & detail.keys()
        event_detail = client.get(f"/api/plugins/ancor/events/{event['id']}").json()
        assert event_detail['event']['payload']['reference'] == '[redacted]'
        overview = client.get('/api/plugins/ancor/overview').json()
    exposed = json.dumps([detail, event_detail, overview])
    for value in ('private-session', 'private-call', 'private-target', 'private-destination',
                  'fixture-receipt', 'private-internal'):
        assert value not in exposed
    assert all(p.read_bytes() == data for p, data in before.items())


def test_dashboard_renders_structured_details_without_external_dependencies():
    import pytest
    quickjs = pytest.importorskip('quickjs')
    code = (Path(__file__).parents[1] / 'ancor/dashboard/dist/index.js').read_text()
    ctx = quickjs.Context()
    ctx.eval('''
        var captured;
        var window = {
          __HERMES_PLUGIN_SDK__: {React: {
            createElement: function(type, props) { return {type:type, props:props, children:Array.prototype.slice.call(arguments,2)}; },
            useState: function(v) { return [v,function(){}]; }, useEffect: function(){}
          }},
          __HERMES_PLUGINS__: {register:function(name, component){captured=component;}}
        };
    ''')
    ctx.eval(code)
    result = ctx.eval('JSON.stringify(captured())')
    assert 'ANCOR' in result and '只读请求' in result
