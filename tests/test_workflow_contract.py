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
    assert 'read-only' in skill
    assert 'Do not coordinate, execute, decompose work, or mutate the board.' in text
    assert '`ancor_board list`' in skill and '`ancor_board show`' in skill
    assert '`ancor_notify respond`' in skill
    assert '`ancor_requirement respond`' in skill
    assert 'confirm with the returned `clarify_args` unchanged' in skill
    assert 'action=pause' in skill and 'action=resume' in skill
    assert 'single-line `title`' in skill
    assert '`summary` is optional' in skill
    assert 'Show the title and summary before the exact proposal text' in skill


def test_notification_presentation_is_system_notice_then_interactor_followup():
    coordinator_soul = (ROOT / 'roles/ancor-coordinator/SOUL.md').read_text()
    coordinator_skill = (ROOT / 'skills/ancor-coordination/SKILL.md').read_text()
    interactor_soul = (ROOT / 'roles/ancor-interactor/SOUL.md').read_text()
    interactor_skill = (ROOT / 'skills/ancor-interactor/SKILL.md').read_text()
    assert 'durable system notices' in coordinator_soul
    assert 'not as a chat reply or technical log' in coordinator_soul
    assert 'Write a system notice, not a chat reply' in coordinator_skill
    assert 'how to reply' in coordinator_skill
    assert 'follow-up questions can be asked in the current conversation' in coordinator_skill
    assert 'explain its current state and progress from ANCOR' in interactor_soul
    assert 'A coordinator notification is a durable system notice' in interactor_skill
    assert 'do not expose internal IDs or mechanics unless needed' in interactor_skill


def test_coordinator_checks_constraint_and_evidence_before_asking():
    coordinator_soul = (ROOT / 'roles/ancor-coordinator/SOUL.md').read_text()
    coordinator_skill = (ROOT / 'skills/ancor-coordination/SKILL.md').read_text()
    assert 'external commitments' in coordinator_soul.lower()
    assert 'environment or image identity' in coordinator_soul
    assert 'at most one low-impact, reversible probe' in coordinator_soul
    assert 'a failed probe is evidence for a question' in coordinator_soul
    assert '**Classify the missing item.**' in coordinator_skill
    assert '**Try bounded self-service.**' in coordinator_skill
    assert 'External commitments belong to the human' in coordinator_skill


def test_coordinator_contract_separates_verified_facts_from_guesses():
    skill = (ROOT / 'skills/ancor-coordination/SKILL.md').read_text()
    assert ('`binding_constraints` is only what the requirement, an authorization boundary, '
            'or a verified environment fact imposes') in skill
    assert 'never a guessed command, toolchain or environment choice' in skill


def test_prompts_name_the_six_card_parts():
    from ancor.operations import CARD_PARTS
    coordinator = (ROOT / 'skills/ancor-coordination/SKILL.md').read_text()
    worker_soul = (ROOT / 'roles/ancor-worker/SOUL.md').read_text()
    worker_skill = (ROOT / 'skills/ancor-worker/SKILL.md').read_text()
    for part in CARD_PARTS:
        variants = (part, part.replace('_', ' '))
        assert any(value in coordinator for value in variants), ('coordinator', part)
        assert any(value in worker_soul for value in variants), ('worker soul', part)
    for part in ('binding_constraints', 'advice', 'expected_output', 'evidence'):
        assert part in worker_skill, ('worker skill', part)
    assert '`advice` is optional hints that never bind' in coordinator
    assert 'Advice never binds' in worker_soul
    assert '`advice` is not binding' in worker_skill


def test_interactor_keeps_requirements_at_outcome_altitude():
    skill = (ROOT / 'skills/ancor-interactor/SKILL.md').read_text()
    assert 'proactively raise any question you believe materially affects the requirement' in skill
    assert 'do not fill gaps silently' in skill
    assert 'Do not add or infer unconfirmed facts, decisions, constraints, implementation methods, tests' in skill
    assert "Keep the user's scope, commitment strength, priority, and unresolved decisions" in skill
    assert 'Would the user recognize this as their request?' in skill


def test_interactor_scopes_progress_to_current_work():
    skill = (ROOT / 'skills/ancor-interactor/SKILL.md').read_text()
    assert 'If one requirement or task is already under discussion, report only that work' in skill
    assert 'get its task with read-only `ancor_board list`, `ancor_board show`, or `ancor_board history`' in skill
    assert 'Use `progress_only=true` and a compact all-open table only when the user asks for an overview' in skill
    assert '`待你验收`' in skill
    assert '`ancor_requirement action=accept`' in skill
    assert '`ancor_requirement action=request_changes`' in skill
    assert 'Do not expand history unless asked' in skill
    assert 'Never mutate the board' in skill


def test_requirement_tool_describes_outcome_altitude():
    tools = {}
    ctx = SimpleNamespace(profile_name='ancor-interactor',
                          get_config=lambda key, default=None: default,
                          register_hook=lambda *args: None,
                          register_tool=lambda **kw: tools.update({kw['name']: kw}))
    ancor.register(ctx)
    tool = tools['ancor_requirement']['schema']
    assert 'outcome-oriented brief' in tool['description']
    assert 'keep implementation method and task decomposition out' in tool['description']
    properties = tool['parameters']['properties']
    assert 'Outcome, scope, hard constraints, non-goals' in properties['brief']['description']
    assert 'which execution choices are left open' in properties['summary']['description']


def test_coordinator_skill_has_no_user_conversation_flow():
    skill = (ROOT / 'skills/ancor-coordination/SKILL.md').read_text()
    assert '## User conversation' not in skill
    assert 'never create or confirm requirements' in skill
    assert '`ancor_board create`' in skill and "takes the card's six parts" in skill
    assert 'worker completion is evidence' in skill.lower()
    assert '`requirement.paused`' in skill and '`requirement.resumed`' in skill


def test_coordinator_does_not_assume_worker_can_read_other_cards():
    soul = (ROOT / 'roles/ancor-coordinator/SOUL.md').read_text()
    skill = (ROOT / 'skills/ancor-coordination/SKILL.md').read_text()
    assert 'Workers receive only the task card assigned to them' in soul
    assert 'Parent links are dependency edges, not an information channel' in soul
    assert '## Worker context contract' in skill
    assert 'A worker receives one assigned card' in skill
    assert '`parent_ids` link records an execution dependency; it is not an information channel' in skill
    assert 'ask whether the worker can reach the outcome' in skill
    assert 'Never write "see the parent card"' in skill
    assert 'Do not include a parent-card summary' in skill


def test_coordinator_skill_documents_requirement_notes():
    skill = (ROOT / 'skills/ancor-coordination/SKILL.md').read_text()
    assert 'requirement-scoped notes' in skill
    assert 'At each wake inspect previews' in skill
    assert '`ancor_note`' in skill
    assert '`created_at` and `updated_at` timestamps' in skill
    assert 'Notes are never authoritative' in skill


def test_worker_reads_only_its_own_card():
    soul = (ROOT / 'roles/ancor-worker/SOUL.md').read_text()
    skill = (ROOT / 'skills/ancor-worker/SKILL.md').read_text()
    assert 'Do not read another task' in soul
    assert 'requirement store' in soul
    assert 'carry no context' in soul
    assert '## Parent task results' in soul
    assert '## Recent work by @...' in soul
    assert 'platform-generated' in soul
    assert 'not part of your contract' in soul
    assert 'the card wins' in soul
    assert 'Use `kanban_show` only for your own task ID.' in skill
    assert 'this card is the only coordination context' in skill


def test_worker_skill_covers_card_execution_and_blocking():
    skill = (ROOT / 'skills/ancor-worker/SKILL.md').read_text()
    assert '`<work_contract>`' in skill
    assert '`kanban_complete`' in skill
    assert '`kanban_block`' in skill
    assert '`kanban_request_review`' in skill
    assert 'Blocking ends your run' in skill
    assert 'Before running a prescriptive setup, build or environment command' in skill
    assert 'do not substitute it silently' in skill
    assert 'the method is yours' in skill
    assert 'contract conflict' in skill


def test_worker_reports_contract_mismatches_instead_of_guessing():
    soul = (ROOT / 'roles/ancor-worker/SOUL.md').read_text()
    skill = (ROOT / 'skills/ancor-worker/SKILL.md').read_text()
    assert "report the card's claim, observed evidence" in soul
    assert 'then block if the conflict prevents valid execution' in soul
    assert 'record the deviation in the completion report' in soul
    assert 'do not substitute it silently' in skill
    assert 'record the deviation' in skill


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
                Service(ctx, store).acceptance({'action': 'accept', 'requirement_id': req['id'],
                                                'evidence': {**evidence, field: invalid}})
    class Board:
        def list(self):
            return [{'id': 'task-one', 'status': 'done'}]

        def archive(self, task_id, reason):
            return {'id': task_id, 'status': 'archived'}

    result = Service(ctx, store, board=Board()).acceptance(
        {'action': 'verify', 'requirement_id': req['id'], 'evidence': evidence})
    assert result['status'] == 'awaiting_acceptance'


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
