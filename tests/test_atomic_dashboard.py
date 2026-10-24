import importlib
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ancor.store import Store


def test_read_only_structured_detail_and_audit_redaction(tmp_path, monkeypatch):
    path = tmp_path / 'requirements.db'
    store = Store(path)
    proposal = store.propose_change(None, [{'op': 'create', 'title': 'Report', 'fields': {'outcome': ['Deliver report']}}],
                                    actor='interactor', source={'input': 'Deliver report', 'token': 'secret-source'})
    rid = store.commit_change(proposal['id'], 'private-confirmation')['requirement_id']
    monkeypatch.setenv('ANCOR_STORE_PATH', str(path))
    api = importlib.import_module('ancor.dashboard.plugin_api')
    app = FastAPI()
    app.include_router(api.router, prefix='/api/plugins/ancor')
    before = path.read_bytes()
    with TestClient(app) as client:
        result = client.get('/api/plugins/ancor/requirements/' + rid)
        assert result.status_code == 200
        detail = result.json()
        assert detail['fields']['outcome'][0]['body'] == 'Deliver report'
        assert detail['changes']['count'] == 1
        audit = client.get('/api/plugins/ancor/changes/' + proposal['id']).json()
        assert audit['confirmation_reference'] == '[redacted]'
        assert audit['source']['token'] == '[redacted]'
        assert client.post('/api/plugins/ancor/requirements/' + rid, json={}).status_code == 405
    assert path.read_bytes() == before


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
    assert 'ANCOR' in result and '只读需求' in result
