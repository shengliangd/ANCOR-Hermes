"""Plugin boundaries; native probes run with a disposable Hermes home."""
import asyncio
import importlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


def test_manifest_declares_tools():
    path = Path(__file__).parents[1] / 'ancor/plugin.yaml'
    assert path.exists(), 'manifest missing'
    manifest = path.read_text()
    assert 'provides_tools:' in manifest
    assert '  - transform_tool_result' in manifest


def test_owner_registers_public_api_lifecycle_only_when_enabled():
    import ancor
    handlers = []
    ctx = SimpleNamespace(profile_name='default',
        get_config=lambda key, default=None: True if key == 'gateway_owner' else default,
        register_platform_handler=lambda *args: handlers.append(args))
    ancor.register(ctx)
    assert [x[0] for x in handlers] == ['api_server']


def test_scan_waits_for_connection_and_survives_plugin_registration_teardown(monkeypatch):
    import ancor

    monkeypatch.setattr(ancor, '_SCAN_CONNECTION_POLL_SECONDS', 0.01)

    class Owner:
        profile_name = 'default'

        def __init__(self):
            self.managed = []
            self.connected = None

        def get_config(self, key, default=None):
            return True if key == 'gateway_owner' else default

        def register_platform_handler(self, platform, handler):
            assert platform == 'api_server'
            self.connected = handler

        def spawn_task(self, coro, *, name=None):
            task = asyncio.get_running_loop().create_task(coro, name=name)
            self.managed.append(task)
            return task

    owner = Owner()
    adapter = SimpleNamespace(name='api', is_connected=False)
    started = []
    stop = asyncio.Event()

    async def runner(ctx, live_adapter):
        started.append(live_adapter.name)
        await stop.wait()

    ancor.register(owner, scan_runner=runner)

    async def scenario():
        owner.connected(None, adapter)
        await asyncio.sleep(0)
        assert started == []
        adapter.is_connected = True
        await asyncio.sleep(0.02)
        assert started == ['api']
        assert owner.managed == []  # not tied to the plugin registration ledger

        # Plugin force-reload cancels every supervised task; the scanner must survive it.
        for task in owner.managed:
            task.cancel()
        await asyncio.sleep(0)
        scan = adapter._ancor_scan_task
        assert not scan.done()

        owner.connected(None, adapter)
        await asyncio.sleep(0)
        assert started == ['api']  # reconnect/reload does not fork a second scanner

        stop.set()
        await scan
        assert scan.done()

    asyncio.run(scenario())


def test_confirmation_cannot_be_model_supplied(tmp_path):
    from ancor.service import Service
    from ancor.store import Store
    store = Store(tmp_path / 'state.db')
    draft = store.create_draft('exact brief', 'exact criteria')
    service = Service(configured_context(), store)
    prepared = service.requirement({'action': 'confirm', 'draft_id': draft['id'], 'confirmed': True})
    assert prepared['clarify_args']
    assert store.snapshot()['requirements'] == []
def configured_context():
    return SimpleNamespace(get_config=lambda key, default=None: default)


@pytest.mark.parametrize('brief', ['Complete brief', 'x' * 5000, 'Literal MEDIA:/private/file'])
def test_confirmation_uses_current_chat_without_delivery_configuration(tmp_path, monkeypatch, brief):
    from ancor.service import Service
    from ancor.store import Store
    service = Service(None, Store(tmp_path / 'state.db'))
    draft = service.requirement({'action': 'draft', 'title': 'Confirmation delivery',
                                 'summary': 'Short high-level summary',
                                 'brief': brief, 'acceptance_criteria': 'exact criteria'})
    question = draft['clarify_args']['questions'][0]
    assert Path(draft['draft_file']).read_text() in question['question']
    assert brief in question['question']
    assert not Path(service.store.path + '.presentations.db').exists()
    assert service.pre_tool_call('clarify', draft['clarify_args'], session_id='s', tool_call_id='c') is None
    result = {'outcome': 'submitted', 'responses': [{'question': question['question'],
              'choices_offered': question['choices'], 'status': 'answered',
              'user_response': question['choices'][1]}]}
    service.post_tool_call('clarify', draft['clarify_args'], result, session_id='s', tool_call_id='c')
    assert service.store.snapshot()['requirements'][0]['draft_id'] == draft['id']
    assert not Path(service.store.path + '.presentations.db').exists()


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
        return 'ancor-interactor'
    def get_config(self, key, default=None):
        if key == 'store_path':
            import os
            return os.path.join(os.environ['HOME'], 'requirements.db')
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
draft = service.requirement({'action': 'draft', 'title': 'Native confirmation', 'summary': 'Native summary', 'brief': 'Complete native draft', 'acceptance_criteria': 'Verified native hook result'})
assert 'Complete native draft' in draft['clarify_args']['questions'][0]['question']
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


def test_only_ancor_roles_have_business_tools():
    plugin = importlib.import_module('ancor')
    assert callable(getattr(plugin, 'register', None)), 'plugin registration missing'
    for role, expected in [('ancor-interactor', True), ('ancor-coordinator', True), ('ancor-worker', False), ('default', False)]:
        tools = {}
        ctx = SimpleNamespace(profile_name=role, get_config=lambda key, default=None: default,
                              register_tool=lambda **kw: tools.update({kw['name']: kw}),
                              register_hook=lambda *args: None,
                              register_platform_handler=lambda *args: None)
        plugin.register(ctx)
        assert bool(tools) is expected
        if role == 'ancor-interactor':
            assert set(tools) == {'ancor_requirement', 'ancor_get_context', 'ancor_board', 'ancor_notify'}
            assert tools['ancor_requirement']['schema']['parameters']['properties']['action']['enum'] == [
                'draft', 'confirm', 'cancel', 'pause', 'resume', 'respond',
                'accept', 'request_changes']
            forbidden = json.loads(tools['ancor_requirement']['handler']({
                'action': 'advance', 'requirement_id': 'fixture'}))
            assert forbidden['code'] == 'forbidden'
            assert tools['ancor_notify']['schema']['parameters']['properties']['action']['enum'] == [
                'list', 'respond']
            assert set(tools['ancor_get_context']['schema']['parameters']['properties']) == {
                'requirement_id', 'task_limit', 'notification_limit',
                'attention_limit', 'include_done', 'include_archived',
                'include_closed_notifications', 'progress_only'}
            assert tools['ancor_board']['schema']['parameters']['properties']['action']['enum'] == ['list', 'show', 'history']
        elif role == 'ancor-coordinator':
            assert set(tools) == {'ancor_requirement', 'ancor_get_context',
                                  'ancor_ack_events', 'ancor_acceptance',
                                  'ancor_board', 'ancor_wake', 'ancor_notify',
                                  'ancor_note'}
            assert tools['ancor_requirement']['schema']['parameters']['properties']['action']['enum'] == ['advance']
            forbidden = json.loads(tools['ancor_requirement']['handler']({
                'action': 'draft', 'title': 'No', 'brief': 'No',
                'acceptance_criteria': 'No'}))
            assert forbidden['code'] == 'forbidden'
            assert tools['ancor_notify']['schema']['parameters']['properties']['action']['enum'] == ['send', 'list', 'withdraw']
            assert 'contract' in tools['ancor_board']['schema']['parameters']['properties']
            assert tools['ancor_note']['schema']['parameters']['properties']['action']['enum'] == [
                'list', 'get', 'add', 'update', 'delete']
        elif role == 'ancor-worker':
            assert not tools


def confirmation(tmp_path):
    from ancor.service import Service
    from ancor.store import Store
    service = Service(configured_context(), Store(tmp_path / 'state.db'))
    draft = service.requirement({'action': 'draft', 'title': 'Exact confirmation',
                                 'summary': 'High-level summary',
                                 'brief': 'Complete brief\nline two', 'acceptance_criteria': 'exact criteria'})
    args = draft['clarify_args']
    question = args['questions'][0]
    result = {'outcome': 'submitted', 'responses': [{'question': question['question'],
              'choices_offered': question['choices'], 'status': 'answered',
              'user_response': question['choices'][1]}]}
    return service, draft, args, result


def confirmation_row(service, session_id='s', tool_call_id='c'):
    db = sqlite3.connect(service.store.path + '.confirmations.db')
    try:
        return db.execute('SELECT consumed, outcome, error FROM confirmations '
                          'WHERE session=? AND call=?',
                          (session_id, tool_call_id)).fetchone()
    finally:
        db.close()


def test_confirmation_survives_service_restart_and_is_exactly_once(tmp_path):
    from ancor.service import Service
    service, draft, args, result = confirmation(tmp_path)
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    restarted = Service(configured_context(), service.store)
    restarted.post_tool_call('clarify', args, json.dumps(result), session_id='other', tool_call_id='c')
    restarted.post_tool_call('clarify', args, json.dumps(result), session_id='s', tool_call_id='other')
    assert not service.store.snapshot()['requirements']
    restarted.post_tool_call('clarify', args, json.dumps(result), session_id='s', tool_call_id='c')
    restarted.post_tool_call('clarify', args, json.dumps(result), session_id='s', tool_call_id='c')
    snapshot = service.store.snapshot()
    assert len(snapshot['requirements']) == len(snapshot['pending_events']) == 1
    assert snapshot['requirements'][0]['draft_id'] == draft['id']
    assert Path(draft['draft_file']).read_text().endswith('exact criteria')
    assert confirmation_row(service) == (1, 'confirmed', None)


def test_confirmation_leads_with_high_level_summary_then_exact_text(tmp_path):
    service, draft, args, result = confirmation(tmp_path)
    question = args['questions'][0]['question']
    assert question.index('High-level summary:') < question.index('Exact requirement to be stored:')
    assert question.index('Exact requirement to be stored:') < question.index('Complete brief\nline two')
    assert 'High-level summary\n' in question


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
    consumed, outcome, error = confirmation_row(service)
    expected = 'error' if mutation in {'error', 'malformed'} else 'rejected'
    assert consumed == 1 and outcome == expected and error


def test_confirmation_store_failure_is_audited(tmp_path, monkeypatch):
    service, draft, args, result = confirmation(tmp_path)

    def fail(*_args):
        raise RuntimeError('store unavailable')

    monkeypatch.setattr(service.store, 'confirm', fail)
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')
    assert not service.store.snapshot()['requirements']
    assert confirmation_row(service) == (1, 'error', 'store unavailable')


def test_confirmation_accepts_additive_response_fields(tmp_path):
    service, draft, args, result = confirmation(tmp_path)
    result['responses'][0]['surface_metadata'] = {'render': 'interactive_card'}
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')
    assert service.store.snapshot()['requirements'][0]['draft_id'] == draft['id']
    assert confirmation_row(service) == (1, 'confirmed', None)


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


def test_immutable_draft_file_cannot_be_changed(tmp_path):
    service, draft, args, result = confirmation(tmp_path)
    Path(draft['draft_file']).write_text('changed')
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c')['action'] == 'block'
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')
    assert not service.store.snapshot()['requirements']


def cancellation(tmp_path):
    from ancor.service import Service
    from ancor.store import Store
    service = Service(configured_context(), Store(tmp_path / 'state.db'))
    requirement = service.store.confirm(
        service.store.create_draft('Cancel this brief', 'No work remains')['id'], 'fixture')
    proposal = service.requirement({
        'action': 'cancel', 'requirement_ids': [requirement['id']],
        'summary': 'Stop the confirmed work', 'reason': 'User withdrew it'})
    args = proposal['clarify_args']
    question = args['questions'][0]
    result = {'outcome': 'submitted', 'responses': [{'question': question['question'],
              'choices_offered': question['choices'], 'status': 'answered',
              'user_response': question['choices'][1]}]}
    return service, requirement, proposal, args, result


def test_cancellation_requires_exact_confirmation_and_is_idempotent(tmp_path):
    service, requirement, proposal, args, result = cancellation(tmp_path)
    assert service.store.get_requirement(requirement['id'])['status'] == 'active'
    assert 'ANCOR cancellation ' + proposal['id'] in args['questions'][0]['question']
    question = args['questions'][0]['question']
    assert question.index('High-level summary:') < question.index('Exact cancellation to be stored:')
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    service.post_tool_call('clarify', args, json.dumps(result), session_id='s', tool_call_id='c')
    service.post_tool_call('clarify', args, json.dumps(result), session_id='s', tool_call_id='c')
    assert service.store.get_requirement(requirement['id'])['target_status'] == 'cancelled'
    service.store.finalize_transition(requirement['id'])
    assert service.store.get_requirement(requirement['id'])['status'] == 'cancelled'
    cancelled = [event for event in service.store.pending_events()
                 if event['kind'] == 'requirement.cancelled']
    assert len(cancelled) == 1
    assert cancelled[0]['payload']['reason'] == 'User withdrew it'


@pytest.mark.parametrize('mutation', ['cancelled', 'timed_out', 'wrong_id', 'wrong_question', 'error'])
def test_nonconsent_cancellation_results_never_cancel(tmp_path, mutation):
    import copy
    service, requirement, proposal, args, valid = cancellation(tmp_path)
    service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c')
    result = copy.deepcopy(valid)
    if mutation in {'cancelled', 'timed_out'}:
        result['outcome'] = mutation
    elif mutation == 'wrong_id':
        result['responses'][0]['user_response'] = 'Cancel ANCOR requirements ' + '0' * 32
    elif mutation == 'wrong_question':
        result['responses'][0]['question'] = 'Cancel?'
    elif mutation == 'error':
        result = {'error': 'unavailable'}
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')
    service.post_tool_call('clarify', args, valid, session_id='s', tool_call_id='c')
    assert service.store.get_requirement(requirement['id'])['status'] == 'active'


def pause_proposal(tmp_path, action='pause'):
    from ancor.service import Service
    from ancor.store import Store
    service = Service(configured_context(), Store(tmp_path / 'state.db'))
    requirement = service.store.confirm(
        service.store.create_draft('Pause this brief', 'No new work')['id'], 'fixture')
    if action == 'resume':
        first = service.requirement({
            'action': 'pause', 'requirement_id': requirement['id'],
            'summary': 'Stop briefly', 'reason': 'User asked'})
        args = first['clarify_args']
        question = args['questions'][0]
        service.pre_tool_call('clarify', args, session_id='pause-s', tool_call_id='pause-c')
        service.post_tool_call('clarify', args, json.dumps({
            'outcome': 'submitted', 'responses': [{
                'question': question['question'], 'choices_offered': question['choices'],
                'status': 'answered', 'user_response': question['choices'][1]}]}),
            session_id='pause-s', tool_call_id='pause-c')
        service.store.finalize_transition(requirement['id'])
    proposal = service.requirement({
        'action': action, 'requirement_id': requirement['id'],
        'summary': 'Resume the confirmed work' if action == 'resume' else 'Stop briefly',
        'reason': 'User is ready' if action == 'resume' else 'User asked'})
    args = proposal['clarify_args']
    question = args['questions'][0]
    result = {'outcome': 'submitted', 'responses': [{'question': question['question'],
              'choices_offered': question['choices'], 'status': 'answered',
              'user_response': question['choices'][1]}]}
    return service, requirement, proposal, args, result


def test_pause_and_resume_require_exact_confirmation_and_are_idempotent(tmp_path):
    service, requirement, proposal, args, result = pause_proposal(tmp_path)
    assert service.store.get_requirement(requirement['id'])['status'] == 'active'
    assert 'ANCOR pause ' + proposal['id'] in args['questions'][0]['question']
    question = args['questions'][0]['question']
    assert question.index('High-level summary:') < question.index('Exact pause to be stored:')
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    service.post_tool_call('clarify', args, json.dumps(result), session_id='s', tool_call_id='c')
    service.post_tool_call('clarify', args, json.dumps(result), session_id='s', tool_call_id='c')
    assert service.store.get_requirement(requirement['id'])['target_status'] == 'paused'
    service.store.finalize_transition(requirement['id'])
    assert service.store.get_requirement(requirement['id'])['status'] == 'paused'
    paused = [event for event in service.store.pending_events()
              if event['kind'] == 'requirement.paused']
    assert len(paused) == 1
    assert paused[0]['payload']['reason'] == 'User asked'

    resume_service, resume_requirement, resume, resume_args, resume_result = pause_proposal(
        tmp_path, action='resume')
    assert resume_service.store.get_requirement(resume_requirement['id'])['status'] == 'paused'
    assert resume_service.pre_tool_call('clarify', resume_args, session_id='s2', tool_call_id='c2') is None
    resume_service.post_tool_call('clarify', resume_args, json.dumps(resume_result),
                                  session_id='s2', tool_call_id='c2')
    resume_service.post_tool_call('clarify', resume_args, json.dumps(resume_result),
                                  session_id='s2', tool_call_id='c2')
    assert resume_service.store.get_requirement(resume_requirement['id'])['target_status'] == 'active'
    resume_service.store.finalize_transition(resume_requirement['id'])
    assert resume_service.store.get_requirement(resume_requirement['id'])['status'] == 'active'
    resumed = [event for event in resume_service.store.pending_events()
               if event['kind'] == 'requirement.resumed']
    assert len(resumed) == 1


@pytest.mark.parametrize('mutation', ['cancelled', 'timed_out', 'wrong_id', 'wrong_question', 'error'])
def test_nonconsent_pause_results_never_change_status(tmp_path, mutation):
    import copy
    service, requirement, proposal, args, valid = pause_proposal(tmp_path)
    service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c')
    result = copy.deepcopy(valid)
    if mutation in {'cancelled', 'timed_out'}:
        result['outcome'] = mutation
    elif mutation == 'wrong_id':
        result['responses'][0]['user_response'] = 'Pause ANCOR requirement ' + '0' * 32
    elif mutation == 'wrong_question':
        result['responses'][0]['question'] = 'Pause?'
    elif mutation == 'error':
        result = {'error': 'unavailable'}
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')
    service.post_tool_call('clarify', args, valid, session_id='s', tool_call_id='c')
    assert service.store.get_requirement(requirement['id'])['status'] == 'active'
