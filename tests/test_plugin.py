"""Plugin boundaries; native probes run with a disposable Hermes home."""
import importlib
import json
from pathlib import Path
import pytest


def test_manifest_declares_tools():
    path = Path(__file__).parents[1] / 'ancor/plugin.yaml'
    assert path.exists(), 'manifest missing'
    assert 'provides_tools:' in path.read_text()


def test_owner_registers_public_api_lifecycle_only_when_enabled():
    import ancor
    handlers = []
    ctx = SimpleNamespace(profile_name='default',
        get_config=lambda key, default=None: True if key == 'gateway_owner' else default,
        register_platform_handler=lambda *args: handlers.append(args))
    ancor.register(ctx)
    assert [x[0] for x in handlers] == ['api_server']


def test_confirmation_cannot_be_model_supplied(tmp_path):
    from ancor.service import Service
    from ancor.store import Store
    store = Store(tmp_path / 'state.db')
    draft = store.create_draft('exact brief', 'exact criteria')
    service = Service(configured_context(), store)
    prepared = service.requirement({'action': 'confirm', 'draft_id': draft['id'], 'confirmed': True})
    assert prepared['clarify_args']
    assert store.snapshot()['requirements'] == []
from types import SimpleNamespace


def configured_context():
    config = {'human_target': 'feishu:test-operator', 'transport_profile': 'default'}
    return SimpleNamespace(get_config=lambda key, default=None: config.get(key, default))


@pytest.fixture(autouse=True)
def native_send_boundary(monkeypatch):
    import subprocess
    original = subprocess.run
    def send(argv, **kwargs):
        if argv[0] != 'hermes':
            return original(argv, **kwargs)
        assert argv == ['hermes', '--profile', 'default', 'send', '--to',
                        'feishu:test-operator', '--file', '-', '--json']
        assert 'Requirement:' in kwargs['input']
        return SimpleNamespace(returncode=0, stdout=json.dumps({'success': True}))
    monkeypatch.setattr('ancor.notification.subprocess.run', send)


def test_native_registration_and_nested_clarify(tmp_path):
    import os
    import subprocess
    import sys
    native = Path('/home/user/.hermes/hermes-agent')
    if not native.is_dir():
        pytest.skip('installed native source unavailable')
    script = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.plugins import PluginContext, PluginManager, get_plugin_manager
from hermes_cli.plugins_manifest import PluginManifest
from tools.registry import registry
import tools.clarify_tool
import ancor
manager = get_plugin_manager()
class Context(PluginContext):
    @property
    def profile_name(self):
        return 'ancor-leader'
    def get_config(self, key, default=None):
        if key == 'store_path':
            import os
            return os.path.join(os.environ['HOME'], 'requirements.db')
        if key == 'human_target':
            return 'feishu:test-operator'
        if key == 'transport_profile':
            return 'default'
        return default
ctx = Context(PluginManifest(name='ancor'), manager)
ancor.register(ctx)
assert registry.get_entry('ancor_requirement', scope=manager.scope_key)
# Even parent_agent is not translated into clarify's callback by registry dispatch.
from types import SimpleNamespace
agent = SimpleNamespace(clarify_callback=lambda questions: {'answers': {'q1': 'Yes'}, 'outcome': 'submitted'})
result = json.loads(ctx.dispatch_tool('clarify', {'questions': [{'question': 'Confirm?', 'choices': ['Yes', 'No']}]}, parent_agent=agent))
assert 'error' in result, result
print('native registration passed; nested clarify unavailable:', result['error'])
# Real native agent tool execution path, deterministic UI callback (no provider or human E2E claim).
from agent.agent_runtime_helpers import invoke_tool
from ancor.service import Service
service = Service(ctx)
from unittest.mock import patch
with patch('ancor.notification.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{"success": true}')) as sender:
    draft = service.requirement({'action': 'draft', 'brief': 'Complete native draft', 'acceptance_criteria': 'Verified native hook result'})
assert sender.call_count == 1
assert 'Complete native draft' in sender.call_args.kwargs['input']
assert 'Complete native draft' not in draft['clarify_args']['questions'][0]['question']
seen = []
ctx.register_hook('post_tool_call', lambda **payload: seen.append(payload))
agent = SimpleNamespace(session_id='native-session', _memory_manager=None, clarify_callback=lambda questions: {
    'answers': {'q0': questions[0]['choices_offered'][1]}, 'outcome': 'submitted'})
result = invoke_tool(agent, 'clarify', draft['clarify_args'], '', tool_call_id='native-call')
assert json.loads(result)['outcome'] == 'submitted', result
snapshot = service.store.snapshot()
assert len(snapshot['requirements']) == 1, snapshot
assert snapshot['requirements'][0]['draft_id'] == draft['id']
assert seen[-1]['session_id'] == 'native-session'
assert seen[-1]['tool_call_id'] == 'native-call'
assert seen[-1]['result'] == result
print('native agent clarify pre/post integration passed')
'''
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path / '.hermes'))
    # Preserve dependency paths before moving HOME; do not inherit live state.
    env['PYTHONPATH'] = os.pathsep.join(p for p in sys.path if p.endswith('site-packages'))
    interpreter = native / '.venv/bin/python'
    result = subprocess.run([str(interpreter), '-c', script, str(native)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_only_leader_has_business_tools():
    plugin = importlib.import_module('ancor')
    assert callable(getattr(plugin, 'register', None)), 'plugin registration missing'
    for role, expected in [('ancor-leader', True), ('ancor-worker', True), ('default', False)]:
        tools = {}
        ctx = SimpleNamespace(profile_name=role, get_config=lambda key, default=None: default,
                              register_tool=lambda **kw: tools.update({kw['name']: kw}),
                              register_hook=lambda *args: None,
                              register_platform_handler=lambda *args: None)
        plugin.register(ctx)
        assert bool(tools) is expected
        if role == 'ancor-leader':
            assert {'ancor_requirement', 'ancor_get_context', 'ancor_acceptance'} <= tools.keys()
        elif role == 'ancor-worker':
            assert set(tools) == {'ancor_get_context'}
            assert 'error' in json.loads(tools['ancor_get_context']['handler']({'ack_event_ids': [1]}))


def confirmation(tmp_path):
    from ancor.service import Service
    from ancor.store import Store
    service = Service(configured_context(), Store(tmp_path / 'state.db'))
    draft = service.requirement({'action': 'draft', 'brief': 'Complete brief\nline two', 'acceptance_criteria': 'exact criteria'})
    args = draft['clarify_args']
    question = args['questions'][0]
    result = {'outcome': 'submitted', 'responses': [{'question': question['question'],
              'choices_offered': question['choices'], 'status': 'answered',
              'user_response': question['choices'][1]}]}
    return service, draft, args, result


def test_confirmation_survives_service_restart_and_is_exactly_once(tmp_path):
    from ancor.service import Service
    service, draft, args, result = confirmation(tmp_path)
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    restarted = Service(configured_context(), service.store)
    restarted.post_tool_call('clarify', args, json.dumps(result), session_id='other', tool_call_id='c')
    assert not service.store.snapshot()['requirements']
    restarted.post_tool_call('clarify', args, json.dumps(result), session_id='s', tool_call_id='c')
    restarted.post_tool_call('clarify', args, json.dumps(result), session_id='s', tool_call_id='c')
    snapshot = service.store.snapshot()
    assert len(snapshot['requirements']) == len(snapshot['pending_events']) == 1
    assert snapshot['requirements'][0]['draft_id'] == draft['id']
    assert Path(draft['draft_file']).read_text().endswith('exact criteria')


@pytest.mark.parametrize('mutation', ['cancelled', 'timed_out', 'undelivered', 'skipped', 'wrong_id', 'wrong_question', 'wrong_choices', 'error', 'malformed'])
def test_nonconsent_results_never_activate_or_replay(tmp_path, mutation):
    import copy
    service, draft, args, valid = confirmation(tmp_path)
    service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c')
    result = copy.deepcopy(valid)
    if mutation in ('cancelled', 'timed_out', 'undelivered'):
        result['outcome'] = mutation
    elif mutation == 'skipped':
        result['responses'][0]['status'] = 'skipped'
    elif mutation == 'wrong_id':
        result['responses'][0]['user_response'] = 'Confirm ANCOR draft ' + '0' * 32
    elif mutation == 'wrong_question':
        result['responses'][0]['question'] = 'Confirm?'
    elif mutation == 'wrong_choices':
        result['responses'][0]['choices_offered'] = ['Yes']
    elif mutation == 'error':
        result = {'error': 'unavailable'}
    elif mutation == 'malformed':
        result = 'not json'
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')
    service.post_tool_call('clarify', args, valid, session_id='s', tool_call_id='c')
    assert not service.store.snapshot()['requirements']


def test_pre_hook_rejects_incomplete_presentation_and_missing_identity(tmp_path):
    import copy
    service, draft, args, result = confirmation(tmp_path)
    assert service.pre_tool_call('clarify', args)['action'] == 'block'
    altered = copy.deepcopy(args)
    altered['questions'][0]['question'] = 'ANCOR draft confirm?'
    assert service.pre_tool_call('clarify', altered, session_id='s', tool_call_id='c')['action'] == 'block'
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')
    assert not service.store.snapshot()['requirements']
    assert service.pre_tool_call('clarify', {'questions': [{'question': 'Ordinary clarification?'}]}) is None


@pytest.mark.parametrize('failure', ['timeout', 'failure', 'malformed', 'skipped'])
def test_unverified_send_never_offers_confirmation(tmp_path, monkeypatch, failure):
    import subprocess
    from ancor.service import Service
    from ancor.store import Store
    service = Service(configured_context(), Store(tmp_path / 'state.db'))
    draft = service.store.create_draft('immutable brief', 'criteria')
    calls = []
    def send(*args, **kwargs):
        calls.append(kwargs['input'])
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('hermes', 60)
        payload = {'success': False} if failure == 'failure' else {'success': True, 'skipped': True}
        return SimpleNamespace(returncode=0, stdout='bad json' if failure == 'malformed' else json.dumps(payload))
    monkeypatch.setattr('ancor.notification.subprocess.run', send)
    for _ in range(2):
        with pytest.raises(ValueError, match='unverified'):
            service.presentation(draft)
    assert len(calls) == 1  # uncertain delivery is not blindly retried
    with pytest.raises(ValueError, match='unverified'):
        service.presentation(draft, deliver=False)
    assert not service.store.snapshot()['requirements']


def test_missing_configuration_and_oversized_draft_fail_closed(tmp_path):
    from ancor.service import Service
    from ancor.store import Store
    store = Store(tmp_path / 'state.db')
    draft = store.create_draft('brief', 'criteria')
    with pytest.raises(ValueError, match='configure human_target'):
        Service(None, store).presentation(draft)
    for brief in ['x' * 2000, 'MEDIA:/private/file']:
        with pytest.raises(ValueError, match='2000 characters'):
            Service(configured_context(), store).presentation(store.create_draft(brief, 'criteria'))
    assert not store.snapshot()['requirements']


@pytest.mark.parametrize('phase', ['pre', 'post'])
def test_receipt_required_at_both_confirmation_boundaries(tmp_path, phase):
    import sqlite3
    service, draft, args, result = confirmation(tmp_path)
    if phase == 'post':
        assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    with sqlite3.connect(service.store.path + '.presentations.db') as db:
        db.execute("UPDATE human_notifications SET receipt=NULL")
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='other')['action'] == 'block'
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')
    assert not service.store.snapshot()['requirements']
