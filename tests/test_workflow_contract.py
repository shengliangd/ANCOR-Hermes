"""Regression contracts for the direct worker-completion workflow."""
from pathlib import Path
from types import SimpleNamespace

import ancor

ROOT = Path(__file__).parents[1]


def test_worker_completes_without_native_review():
    text = (ROOT / 'roles/ancor-worker/SOUL.md').read_text()
    assert '`kanban_complete`' in text
    assert 'Do not call `kanban_request_review`' in text


def test_setup_disables_native_review_dispatch():
    setup = (ROOT / 'SETUP.md').read_text()
    assert 'kanban.review_dispatch false' in setup
    assert 'Gateway owner' in setup


def test_acceptance_schema_matches_service_contract(tmp_path):
    import pytest
    from ancor.service import Service
    from ancor.store import Store
    tools = {}
    ctx = SimpleNamespace(profile_name='ancor-coordinator',
                          get_config=lambda key, default=None: default,
                          register_hook=lambda *args: None,
                          register_tool=lambda **kw: tools.update({kw['name']: kw}))
    ancor.register(ctx)
    schema = tools['ancor_acceptance']['schema']['parameters']['properties']['evidence']
    assert set(schema['required']) == {'criteria', 'verification', 'artifacts', 'task_ids'}
    store = Store(tmp_path / 'store.db')
    draft = store.create_draft('brief', 'exact criteria')
    req = store.confirm(draft['id'], 'fixture')
    store.link_task(req['id'], 'task-one')
    evidence = {'criteria': 'exact criteria', 'verification': ['checked artifact'],
                'artifacts': ['/result.txt'], 'task_ids': ['task-one']}
    assert schema['properties']['criteria']['type'] == 'string'
    for field in ('verification', 'artifacts', 'task_ids'):
        field_schema = schema['properties'][field]
        assert field_schema['type'] == 'array'
        assert field_schema['minItems'] == 1
        assert field_schema['items']['type'] == 'string'
        assert field_schema['items']['pattern'] == r'\S'
        for invalid in ('string', [], [1], ['   ']):
            with pytest.raises(ValueError):
                Service(ctx, store).acceptance({'action': 'accept', 'requirement_id': req['id'],
                                                'evidence': {**evidence, field: invalid}})
    result = Service(ctx, store).acceptance({'action': 'accept', 'requirement_id': req['id'], 'evidence': evidence})
    assert result['status'] == 'accepted'


def test_manifest_uses_native_setting_mapping(tmp_path):
    import os
    import subprocess
    import pytest
    native = Path('/home/user/.hermes/hermes-agent')
    if not native.is_dir():
        pytest.skip('native Hermes unavailable')
    script = '''
import sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.plugins_manifest import validate_config_schema
from hermes_cli.plugins_settings import _manifest_config_schema
from pathlib import Path
schema = _manifest_config_schema(Path(sys.argv[2]))
assert 'store_path' in schema, schema
assert 'properties' not in schema and 'type' not in schema
assert not validate_config_schema('ancor', schema, {'gateway_owner': False, 'poll_interval': 15})
for field, bad in [('gateway_owner', 'false'), ('poll_interval', '15'), ('store_path', 42)]:
    warnings = validate_config_schema('ancor', schema, {field: bad})
    assert len(warnings) == 1 and '.settings.' + field in warnings[0], warnings
'''
    import site
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path / '.hermes'),
               PYTHONPATH=site.getusersitepackages())
    result = subprocess.run([str(native / '.venv/bin/python'), '-c', script,
                             str(native), str(ROOT / 'ancor')],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'under `plugins.entries.ancor.settings`' in (ROOT / 'SETUP.md').read_text()
