"""Native registry dispatch, real durable state, synthetic sender only."""
import os
from pathlib import Path
import subprocess

import pytest

NATIVE = Path('/home/user/.hermes/hermes-agent')


def test_notifications_native_registry(tmp_path):
    if not NATIVE.is_dir():
        pytest.skip('native Hermes unavailable')
    script = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.plugins import PluginContext, get_plugin_manager
from hermes_cli.plugins_manifest import PluginManifest
from tools.registry import registry
import ancor
from ancor.notification import HermesSender, NotificationOutbox
from ancor.store import Store
sent = []
def send(self, target, message):
    sent.append((self.profile, target, message))
    return {'success': True, 'message_id': 'synthetic'}
HermesSender.__call__ = send
config = {'store_path': os.path.join(os.environ['HOME'], 'state.db'),
          'human_target': 'feishu:synthetic', 'transport_profile': 'default'}
manager = get_plugin_manager()
class Context(PluginContext):
    @property
    def profile_name(self): return 'ancor-leader'
    def get_config(self, key, default=None): return config.get(key, default)
ctx = Context(PluginManifest(name='ancor'), manager)
ancor.register(ctx)
assert registry.get_entry('ancor_notify', scope=manager.scope_key), 'notification tool missing'
def call(action, **args):
    return json.loads(ctx.dispatch_tool('ancor_notify', dict(action=action, **args)))
question = dict(id='question-1', kind='question', message='Exact question?')
os.environ.pop('ANCOR_COORDINATOR', None)
assert 'coordinator' in call('send', **question)['error']
os.environ['ANCOR_COORDINATOR'] = '1'
assert call('send', **question)['state'] == 'sent'
assert call('send', **question)['state'] == 'sent'
assert sent == [('default', 'feishu:synthetic', 'Exact question?')]
assert 'error' in call('send', **dict(question, message='changed'))
os.environ.pop('ANCOR_COORDINATOR')
answer = '  Exact answer\nnot authorization  '
result = call('respond', id='question-1', answer=answer)
assert result['notification']['answer'] == answer, result
assert result['event']['payload']['authorization'] is False
assert result['event']['payload']['source'] == 'leader_transcription'
assert call('respond', id='question-1', answer=answer)['event']['id'] == result['event']['id']
assert 'error' in call('respond', id='question-1', answer='different')
store = Store(config['store_path'])
assert not store.snapshot()['requirements']
# Simulate crash after durable reply and before the Store event transaction.
outbox = NotificationOutbox(store.path + '.notifications.db', leader_profile='ancor-leader',
                            target=config['human_target'], sender=lambda *a: None)
outbox.enqueue('crash-question', 'question', 'Recovery?', actor='ancor-leader')
outbox.respond('crash-question', 'recover me', 'leader_transcription:crash-question')
ancor.register(ctx)
context = json.loads(ctx.dispatch_tool('ancor_get_context', {}))
assert len(context['notifications']) == 2
responses = [e for e in context['pending_events'] if e['kind'] == 'notification.response']
assert len(responses) == 2
json.loads(ctx.dispatch_tool('ancor_get_context', {}))
assert len([e for e in store.pending_events() if e['kind'] == 'notification.response']) == 2
os.environ['ANCOR_COORDINATOR'] = '1'
draft = store.create_draft('brief', 'criteria')
req = store.confirm(draft['id'], 'synthetic-fixture')
completion = dict(id='accept:' + req['id'], kind='completion', message='Done', requirement_id=req['id'])
assert 'accepted' in call('send', **completion)['error']
store.accept(req['id'], 'synthetic evidence')
assert 'error' in call('send', **dict(completion, id='unlinked'))
assert call('send', **completion)['state'] == 'sent'
assert 'error' in call('respond', id=completion['id'], answer='yes')
assert store.get_requirement(req['id'])['status'] == 'accepted'
assert len(call('list')['notifications']) == 3
# Runtime metadata is an audit link only; model provenance is not a schema field.
entry = registry.get_entry('ancor_notify', scope=manager.scope_key)
assert 'reference' not in entry.schema['parameters']['properties']
call('send', id='metadata', kind='question', message='Metadata?')
result = json.loads(ctx.dispatch_tool('ancor_notify', {
    'action': 'respond', 'id': 'metadata', 'answer': 'answer'},
    session_id='native-session', tool_call_id='native-call'))
assert result['event']['payload']['authorization'] is False
# Native registry may omit metadata when its dispatch contract does not forward it.
reference = result['notification']['answer_reference']
assert reference == 'leader_transcription:metadata' or json.loads(reference) == {
    'session_id': 'native-session', 'tool_call_id': 'native-call'}
# Uncertain delivery remains durable and is never automatically retried.
def failed_send(self, target, message):
    raise TimeoutError('synthetic timeout')
HermesSender.__call__ = failed_send
uncertain = dict(id='uncertain', kind='question', message='Uncertain?')
assert call('send', **uncertain)['state'] == 'uncertain'
HermesSender.__call__ = send
assert call('send', **uncertain)['state'] == 'uncertain'
assert len(sent) == 3
# Other roles do not register notification tools or expose notification context.
class Worker(Context):
    @property
    def profile_name(self): return 'ancor-worker'
    def register_tool(self, **kw): worker_tools[kw['name']] = kw
worker_tools = {}
ancor.register(Worker(PluginManifest(name='worker-test'), manager))
assert set(worker_tools) == {'ancor_get_context'}
assert 'notifications' not in json.loads(worker_tools['ancor_get_context']['handler']({}))
class Owner(Worker):
    @property
    def profile_name(self): return 'default'
worker_tools.clear()
ancor.register(Owner(PluginManifest(name='owner-test'), manager))
assert not worker_tools
# Missing explicit routing fails closed, with context still recoverable.
config.pop('transport_profile')
ancor.register(ctx)
assert 'error' in call('send', id='no-route', kind='question', message='Never sent')
assert len(sent) == 3
context = json.loads(ctx.dispatch_tool('ancor_get_context', {}))
assert context['requirements'] and context['notifications_error']
print('notification integration verified')
'''
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path / '.hermes'))
    import site
    env['PYTHONPATH'] = site.getusersitepackages()
    result = subprocess.run([str(NATIVE / '.venv/bin/python'), '-c', script, str(NATIVE)],
                            cwd=Path(__file__).parents[1], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
