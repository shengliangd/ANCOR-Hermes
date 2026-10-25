"""Exercise registered product handlers through native PluginContext dispatch."""
import os
from pathlib import Path
import subprocess
import pytest

NATIVE = Path('/home/user/.hermes/hermes-agent')


def test_native_business_registration_and_gates(tmp_path):
    if not NATIVE.is_dir():
        pytest.skip('native Hermes unavailable')
    script = r'''
import asyncio, json, os, sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.plugins import PluginContext, get_plugin_manager
from hermes_cli.plugins_manifest import PluginManifest
from tools.registry import registry
from ancor.store import Store
from ancor.board import NativeBoard
import ancor
manager = get_plugin_manager()
class Context(PluginContext):
    role = 'ancor-interactor'
    @property
    def profile_name(self): return self.role
    def get_config(self, key, default=None):
        return {'store_path': os.path.join(os.environ['HOME'], 'store.db'), 'board': 'test'}.get(key, default)
ctx = Context(PluginManifest(name='ancor'), manager)
ancor.register(ctx)
def call(name, args):
    return json.loads(ctx.dispatch_tool(name, args))
entry = registry.get_entry('ancor_board', scope=manager.scope_key)
assert entry
draft = call('ancor_request', {'action':'draft', 'title':'Exact brief', 'summary':'High-level summary', 'brief':'exact brief'})
assert draft['clarify_args']
assert call('ancor_request', {'action':'confirm', 'draft_id':draft['id']})['id'] == draft['id']
store = Store(ctx.get_config('store_path'))
assert not store.snapshot()['requests']
# Fixture consent only; genuine native confirmation is covered separately.
req = store.confirm(draft['id'], 'synthetic-fixture')
text = '  Exact user reply\nDo not broaden scope.  '
event = call('ancor_request', {'action':'respond', 'request_id':req['id'], 'body':text})
assert event['payload']['body'] == text and event['payload']['authorization'] is False
assert event['payload']['source'] == 'interactor_transcription'
assert store.get_request(req['id'])['status'] == 'active'
ctx.role = 'ancor-coordinator'
ancor.register(ctx)
entry = registry.get_entry('ancor_board', scope=manager.scope_key)
assert entry
# Context now observes native tasks too; install the subprocess fixture before
# its first read so Operations does not cache an unmocked NativeBoard.
# Replace only the native CLI subprocess boundary, retain NativeBoard and Operations.
tasks = {'one': {'id':'one', 'status':'done', 'body':''},
         'two': {'id':'two', 'status':'blocked', 'body':''}}
from types import SimpleNamespace
def run(argv, **kwargs):
    action = argv[6]
    if action == 'list': value = list(tasks.values())
    elif action == 'show': value = {'task': tasks[argv[7]], 'runs':[], 'comments':[]}
    elif action == 'comment': return SimpleNamespace(stdout='')
    elif action == 'archive':
        tasks[argv[7]]['status'] = 'archived'
        return SimpleNamespace(stdout='')
    else: raise AssertionError(argv)
    return SimpleNamespace(stdout=json.dumps(value))
original = NativeBoard.__init__
def init(self, board, **kwargs): original(self, board, run=run, **kwargs)
NativeBoard.__init__ = init
assert call('ancor_get_context', {})['pending_events']
wake_entry = registry.get_entry('ancor_wake', scope=manager.scope_key)
assert wake_entry
assert call('ancor_wake', {'action': 'list'})['items'] == []
assert 'error' in call('ancor_wake', {'action': 'schedule'})
store.link_task(req['id'], 'one')
store.link_task(req['id'], 'two')
assert len(call('ancor_board', {'action':'list'})['items']) == 2
assert call('ancor_board', {'action':'show', 'task_id':'one'})['id'] == 'one'
evidence = {'brief':'exact brief', 'verification':['checked'], 'artifacts':['result'], 'task_ids':['one']}
args = {'request_id':req['id'], 'action':'accept', 'evidence':evidence}
assert 'all linked native tasks' in call('ancor_acceptance', args)['error']
assert store.get_request(req['id'])['status'] == 'active'
tasks['two']['status'] = 'done'
assert call('ancor_acceptance', args)['status'] == 'awaiting_acceptance'
assert 'error' not in call('ancor_get_context', {'ack_event_ids':[event['id']]})
assert event['id'] not in [e['id'] for e in store.pending_events()]
# Public supervised task handle supports reconnect dedup and restart after exit.
class Owner(Context):
    @property
    def profile_name(self): return 'default'
    def get_config(self, key, default=None): return True if key == 'gateway_owner' else default
    def register_platform_handler(self, platform, handler): self.connected = handler
owner = Owner(PluginManifest(name='ancor-owner'), manager)
async def verify_owner():
    started = []
    release = asyncio.Event()
    async def runner(ctx, adapter):
        started.append(adapter.name)
        await release.wait()
    adapter = SimpleNamespace(name='first', is_connected=True)
    ancor.register(owner, scan_runner=runner)
    owner.connected(None, adapter)
    owner.connected(None, adapter)
    await asyncio.sleep(0)
    assert started == ['first']
    release.set()
    await asyncio.sleep(0)
    owner.connected(None, adapter)
    await asyncio.sleep(0)
    assert started == ['first', 'first']
asyncio.run(verify_owner())
print('native registration, coordination gates, replies, acceptance and pump verified')
'''
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path / '.hermes'))
    import site
    env['PYTHONPATH'] = site.getusersitepackages()
    result = subprocess.run([str(NATIVE / '.venv/bin/python'), '-c', script, str(NATIVE)],
                            cwd=Path(__file__).parents[1], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
