"""Regression contracts for the direct worker-completion workflow."""
from pathlib import Path
from types import SimpleNamespace

import ancor
from native_hermes import native_source_or_skip

ROOT = Path(__file__).parents[1]


def test_role_skills_reference_available_ancor_interfaces():
    import re
    import yaml
    manifest = yaml.safe_load((ROOT / 'ancor/plugin.yaml').read_text())
    available = set(manifest['provides_tools'])
    for path in (ROOT / 'skills').glob('ancor-*/SKILL.md'):
        text = path.read_text()
        metadata = yaml.safe_load(text.split('---', 2)[1])
        assert metadata['name'] == path.parent.name
        assert metadata['description']
        references = set(re.findall(r'\bancor_[a-z_]+\b', text))
        assert references <= available, (path, references - available)


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
    assert set(schema['required']) == {'verification', 'artifacts', 'task_ids'}
    assert schema['anyOf'] == [{'required': ['brief']}, {'required': ['generation', 'coverage']}]
    store = Store(tmp_path / 'store.db')
    draft = store.create_draft('brief')
    req = store.confirm(draft['id'], 'fixture')
    store.link_task(req['id'], 'task-one')
    evidence = {'brief': req['brief'], 'verification': ['checked artifact'],
                'artifacts': ['/result.txt'], 'task_ids': ['task-one']}
    assert schema['properties']['brief']['type'] == 'string'
    for field in ('verification', 'artifacts', 'task_ids'):
        field_schema = schema['properties'][field]
        assert field_schema['type'] == 'array'
        assert field_schema['minItems'] == 1
        assert field_schema['items']['type'] == 'string'
        assert field_schema['items']['pattern'] == r'\S'
        for invalid in ('string', [], [1], ['   ']):
            with pytest.raises(ValueError):
                Service(ctx, store).acceptance({'action': 'accept', 'request_id': req['id'],
                                                'evidence': {**evidence, field: invalid}})
    class Board:
        def list(self):
            return [{'id': 'task-one', 'status': 'done'}]

        def archive(self, task_id, reason):
            return {'id': task_id, 'status': 'archived'}

    result = Service(ctx, store, board=Board()).acceptance(
        {'action': 'verify', 'request_id': req['id'], 'evidence': evidence})
    assert result['status'] == 'awaiting_acceptance'


def test_manifest_uses_native_setting_mapping(tmp_path):
    import os
    import subprocess
    import pytest
    native = native_source_or_skip()
    script = '''
import sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.plugins_manifest import validate_config_schema
from hermes_cli.plugins_settings import _manifest_config_schema
from pathlib import Path
schema = _manifest_config_schema(Path(sys.argv[2]))
assert 'store_path' in schema, schema
assert 'properties' not in schema and 'type' not in schema
assert not validate_config_schema('ancor', schema, {'gateway_owner': False, 'poll_interval': 30})
for field, bad in [('gateway_owner', 'false'), ('poll_interval', '30'), ('store_path', 42)]:
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
