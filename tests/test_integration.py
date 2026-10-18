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
    @property
    def profile_name(self): return 'ancor-leader'
    def get_config(self, key, default=None):
        return {'store_path': os.path.join(os.environ['HOME'], 'store.db'), 'board': 'test'}.get(key, default)
ctx = Context(PluginManifest(name='ancor'), manager)
ancor.register(ctx)
def call(name, args):
    return json.loads(ctx.dispatch_tool(name, args))
entry = registry.get_entry('ancor_board', scope=manager.scope_key)
assert entry
os.environ.pop('ANCOR_COORDINATOR', None)
for action in ('create', 'block', 'unblock', 'comment'):
    assert 'coordinator run required' in call('ancor_board', {'action': action})['error']
for marker in ('0', 'true', ''):
    os.environ['ANCOR_COORDINATOR'] = marker
    assert 'coordinator run required' in call('ancor_acceptance', {'action': 'reject'})['error']
os.environ.pop('ANCOR_COORDINATOR', None)
draft = call('ancor_requirement', {'action':'draft', 'brief':'exact brief', 'acceptance_criteria':'criteria'})
assert draft['clarify_args']
assert call('ancor_requirement', {'action':'confirm', 'draft_id':draft['id']})['id'] == draft['id']
store = Store(ctx.get_config('store_path'))
assert not store.snapshot()['requirements']
# Fixture consent only; genuine native confirmation is covered separately.
req = store.confirm(draft['id'], 'synthetic-fixture')
text = '  Exact user reply\nDo not broaden scope.  '
event = call('ancor_requirement', {'action':'respond', 'requirement_id':req['id'], 'body':text})
assert event['payload']['body'] == text and event['payload']['authorization'] is False
assert event['payload']['source'] == 'leader_transcription'
assert store.get_requirement(req['id'])['status'] == 'active'
assert 'coordinator run required' in call('ancor_requirement', {'action':'link'})['error']
assert 'coordinator run required' in call('ancor_get_context', {'ack_event_ids':[event['id']]})['error']
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
    else: raise AssertionError(argv)
    return SimpleNamespace(stdout=json.dumps(value))
original = NativeBoard.__init__
def init(self, board, **kwargs): original(self, board, run=run, **kwargs)
NativeBoard.__init__ = init
assert call('ancor_get_context', {})['pending_events']
wake_entry = registry.get_entry('ancor_wake', scope=manager.scope_key)
assert wake_entry
assert call('ancor_wake', {'action': 'list'}) == []
assert 'coordinator run required' in call('ancor_wake', {'action': 'schedule'})['error']
store.link_task(req['id'], 'one')
store.link_task(req['id'], 'two')
assert len(call('ancor_board', {'action':'list'})['tasks']) == 2
assert call('ancor_board', {'action':'show', 'task_id':'one'})['task']['id'] == 'one'
os.environ['ANCOR_COORDINATOR'] = '1'
assert 'error' not in call('ancor_requirement', {'action':'link', 'requirement_id':req['id'], 'task_id':'one'})
evidence = {'criteria':'criteria', 'verification':['checked'], 'artifacts':['result'], 'task_ids':['one']}
args = {'requirement_id':req['id'], 'action':'accept', 'evidence':evidence}
assert 'all linked native tasks' in call('ancor_acceptance', args)['error']
assert store.get_requirement(req['id'])['status'] == 'active'
tasks['two']['status'] = 'done'
assert call('ancor_acceptance', args)['status'] == 'accepted'
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
        started.append(adapter)
        await release.wait()
    ancor.register(owner, scan_runner=runner)
    owner.connected(None, 'first')
    owner.connected(None, 'second')
    await asyncio.sleep(0)
    assert started == ['first']
    release.set()
    await asyncio.sleep(0)
    owner.connected(None, 'third')
    await asyncio.sleep(0)
    assert started == ['first', 'third']
asyncio.run(verify_owner())
print('native registration, coordination gates, replies, acceptance and pump verified')
'''
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path / '.hermes'))
    import site
    env['PYTHONPATH'] = site.getusersitepackages()
    result = subprocess.run([str(NATIVE / '.venv/bin/python'), '-c', script, str(NATIVE)],
                            cwd=Path(__file__).parents[1], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
