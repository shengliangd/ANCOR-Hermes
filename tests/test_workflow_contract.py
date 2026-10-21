"""Regression contracts for the direct worker-completion workflow."""
from pathlib import Path
from types import SimpleNamespace

import ancor

ROOT = Path(__file__).parents[1]


def test_worker_completes_without_native_review():
    text = (ROOT / 'roles/ancor-worker/SOUL.md').read_text()
    assert '`kanban_complete`' in text
    assert 'Do not call `kanban_request_review`' in text


def test_interactor_can_read_but_not_mutate_the_board():
    text = (ROOT / 'roles/ancor-interactor/SOUL.md').read_text()
    skill = (ROOT / 'skills/ancor-interactor/SKILL.md').read_text()
    assert 'may read the task board' in text
    assert '`ancor_board list`' in skill and '`ancor_board show`' in skill
    assert 'must not mutate the board' in text
    assert '`ancor_notify respond`' in skill
    assert '`ancor_requirement respond`' in skill
    assert 'The answer becomes durable only after the user submits that exact confirmation.' in skill
    assert 'action=pause' in skill and 'action=resume' in skill
    assert 'authoritative single-line `title`' in skill
    assert '`summary` is optional' in skill
    assert 'show the title and any summary first' in skill


def test_interactor_keeps_requirements_at_outcome_altitude():
    text = (ROOT / 'roles/ancor-interactor/SOUL.md').read_text()
    skill = (ROOT / 'skills/ancor-interactor/SKILL.md').read_text()
    assert 'Do not prescribe implementation method, verification technique, or task decomposition.' in text
    assert '### Requirement altitude' in skill
    assert 'Leave implementation and execution choices to the coordinator and workers' in skill
    assert 'would a different valid implementation still satisfy it' in skill
    assert 'Do not invoke altitude to drop safety, authorization, resource limits' in skill
    assert 'not verification procedures, evidence formats, or internal state assertions' in skill
    assert 'Image-text training must actually work, not merely appear runnable.' in skill
    assert 'Training data must update correctly when the data sources change.' in skill


def test_requirement_tool_describes_outcome_altitude():
    tools = {}
    ctx = SimpleNamespace(profile_name='ancor-interactor',
                          get_config=lambda key, default=None: default,
                          register_hook=lambda *args: None,
                          register_tool=lambda **kw: tools.update({kw['name']: kw}))
    ancor.register(ctx)
    tool = tools['ancor_requirement']['schema']
    assert 'outcome-oriented brief and observable acceptance criteria' in tool['description']
    assert 'keep implementation method and task decomposition out' in tool['description']
    properties = tool['parameters']['properties']
    assert 'Outcome, scope, hard constraints, non-goals' in properties['brief']['description']
    assert 'not test steps, evidence formats or internal implementation details' in properties['acceptance_criteria']['description']
    assert 'which execution choices are left open' in properties['summary']['description']


def test_coordinator_skill_has_no_user_conversation_flow():
    skill = (ROOT / 'skills/ancor-coordination/SKILL.md').read_text()
    assert '## User conversation' not in skill
    assert 'never create or confirm requirements' in skill
    assert '`ancor_board create`' in skill and 'complete `contract`' in skill
    assert 'worker completion is evidence' in skill.lower()
    assert '`requirement.paused`' in skill and '`requirement.resumed`' in skill


def test_worker_skill_covers_card_execution_and_blocking():
    skill = (ROOT / 'skills/ancor-worker/SKILL.md').read_text()
    assert '`<work_contract>`' in skill
    assert '`kanban_complete`' in skill
    assert '`kanban_block`' in skill
    assert '`kanban_request_review`' in skill
    assert 'Triage is a failure state' in skill


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
