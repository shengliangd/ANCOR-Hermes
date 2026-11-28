"""Tests for 2-profile Plan A (default + ancor-exec), role fences, transitions, visualizer & Dashboard."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import ancor
from ancor.dashboard.plugin_api import router as dashboard_router
from ancor.goals import GoalStore
from ancor.host import Host
from ancor.store import Store
from ancor.transitions import advance_request
from ancor.visualizer import render_html, render_text_tree
from ancor.watcher import FileClusterBackend


class FakeContext:
    def __init__(self, profile_name, store_path, **extra):
        self.profile_name = profile_name
        self.config = {
            'store_path': str(store_path),
            'interactor_profile': 'default',
            'exec_profile': 'ancor-exec',
            'owner_profile': 'default',
            'gateway_owner': profile_name == 'default',
            'human_target': 'trial:human',
            **extra,
        }
        self.tools = {}
        self.hooks = {}
        self.platform_handlers = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, *, name, toolset, schema, handler, check_fn=None):
        self.tools[name] = {'toolset': toolset, 'schema': schema, 'handler': handler, 'check_fn': check_fn}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_platform_handler(self, name, fn):
        self.platform_handlers[name] = fn


def _create_req(store: Store) -> dict:
    p = store.propose_change(
        None,
        [{'op': 'create', 'title': 'Toy Research', 'fields': {
            'outcome': ['Complete toy experiment'],
            'constraint': ['Stay within budget'],
        }}],
        actor='default',
        source={'kind': 'interactor_proposal', 'input': 'toy'},
    )
    rid = store.commit_change(p['id'], 'confirm-1')['request_id']
    return store.get_request(rid)


def test_two_profile_role_isolation_and_pre_tool_call_fences(tmp_path, monkeypatch):
    store_path = tmp_path / 'requests.db'
    store = Store(store_path)
    req = _create_req(store)

    # 1. default profile = Interactor + Gateway owner
    monkeypatch.delenv('ANCOR_ROLE', raising=False)
    monkeypatch.delenv('GOAL_ID', raising=False)
    interactor = FakeContext('default', store_path)
    ancor.register(interactor)
    assert 'api_server' in interactor.platform_handlers
    assert {'ancor_change_propose', 'ancor_overview', 'ancor_visualize'} <= set(interactor.tools)
    assert 'ancor_goal_spawn' not in interactor.tools

    # 2. ancor-exec with ANCOR_ROLE=root
    monkeypatch.setenv('ANCOR_ROLE', 'root')
    root = FakeContext('ancor-exec', store_path)
    ancor.register(root)
    assert {'ancor_goal_spawn', 'ancor_delivery_submit', 'ancor_question_create', 'ancor_visualize'} <= set(root.tools)
    assert 'ancor_resource_acquire' not in root.tools
    # Root fence physically blocks terminal / write_file / delegate_task!
    assert root.hooks['pre_tool_call']('terminal')['action'] == 'block'
    assert root.hooks['pre_tool_call']('write_file')['action'] == 'block'
    assert root.hooks['pre_tool_call']('ancor_goal_spawn') is None

    # 3. ancor-exec with ANCOR_ROLE=goal
    monkeypatch.setenv('ANCOR_ROLE', 'goal')
    goal_ctx = FakeContext('ancor-exec', store_path)
    ancor.register(goal_ctx)
    assert {'ancor_request_read', 'ancor_goal_spawn', 'ancor_goal_report', 'ancor_resource_acquire'} <= set(goal_ctx.tools)
    assert 'ancor_question_create' not in goal_ctx.tools
    assert goal_ctx.hooks['pre_tool_call']('clarify')['action'] == 'block'
    assert goal_ctx.hooks['pre_tool_call']('send_message')['action'] == 'block'
    assert goal_ctx.hooks['pre_tool_call']('terminal') is None

    # 4. ancor-exec with ANCOR_ROLE=judge
    monkeypatch.setenv('ANCOR_ROLE', 'judge')
    judge = FakeContext('ancor-exec', store_path)
    ancor.register(judge)
    assert set(judge.tools) == {'ancor_request_read', 'ancor_judge_verdict'}
    assert judge.hooks['pre_tool_call']('write_file')['action'] == 'block'
    assert judge.hooks['pre_tool_call']('terminal')['action'] == 'block'

    # End-to-end tool call + visualizer
    spawn_res = json.loads(root.tools['ancor_goal_spawn']['handler']({
        'goal_id': 'g_toy_1',
        'request_ref': {'id': req['id'], 'generation': 1},
        'objective': 'Run toy benchmark',
        'done_criteria': 'Benchmark JSON ready',
        'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
        'budget': {
            'limits': {'gpu.alaya': 2, 'api_usd': 10, 'wall_hours': 4},
            'allowed': {'clusters': ['alaya']},
            'constraints': ['Prefer off-peak hours'],
        },
    }))
    assert spawn_res['goal_id'] == 'g_toy_1'

    monkeypatch.setenv('GOAL_ID', 'g_toy_1')
    acq = json.loads(goal_ctx.tools['ancor_resource_acquire']['handler']({
        'usage': {'gpu.alaya': 1, 'api_usd': 2}, 'tags': {'clusters': 'alaya'}, 'handle_id': 'h_toy',
    }))
    assert acq['handle_id'] == 'h_toy'

    # Read-only visualizer tool works from default (Interactor)
    viz_out = tmp_path / 'view.html'
    viz = json.loads(interactor.tools['ancor_visualize']['handler']({'output_path': str(viz_out)}))
    assert 'g_toy_1' in viz['tree']
    assert viz_out.exists() and 'Prefer off-peak hours' in viz_out.read_text(encoding='utf-8')


def test_request_transitions_cascade_down_goal_tree(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _create_req(store)
    goals = GoalStore(store)
    backend = FileClusterBackend(tmp_path / 'cluster.json')

    goals.spawn_goal({
        'goal_id': 'l1', 'request_ref': {'id': req['id'], 'generation': 1},
        'objective': 'L1', 'done_criteria': 'Done',
        'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
        'budget': {'limits': {'gpu.alaya': 2, 'wall_hours': 4}, 'allowed': {'clusters': ['alaya']}},
    })
    goals.register_resource_usage('l1', handle_id='job_x', usage={'gpu.alaya': 1}, tags={'clusters': 'alaya'})
    backend.submit_handle(handle_id='job_x', usage={'gpu.alaya': 1}, tags={'clusters': 'alaya'}, goal_id='l1')

    r = store.get_request(req['id'])
    p = store.propose_change(req['id'], [{'op': 'pause', 'reason': 'hold'}],
                             actor='default', source={'kind': 'interactor_proposal', 'input': 'pause'},
                             base={'generation': r['generation'], 'control_version': r['control_version']})
    store.commit_change(p['id'], 'c-pause')
    adv = advance_request(store, goals, req['id'],
                          process_killer=lambda _: True,
                          job_canceller=lambda h: backend.cancel_handle(h['handle_id'], h.get('tags')))
    assert adv['status'] == 'paused'
    assert backend.query_handle('job_x')['status'] == 'running'

    r = store.get_request(req['id'])
    p = store.propose_change(req['id'], [{'op': 'cancel', 'reason': 'abort'}],
                             actor='default', source={'kind': 'interactor_proposal', 'input': 'cancel'},
                             base={'generation': r['generation'], 'control_version': r['control_version']})
    store.commit_change(p['id'], 'c-cancel')
    adv = advance_request(store, goals, req['id'],
                          process_killer=lambda _: True,
                          job_canceller=lambda h: backend.cancel_handle(h['handle_id'], h.get('tags')))
    assert adv['status'] == 'cancelled'
    assert backend.query_handle('job_x')['status'] == 'cancelled'


def test_host_scan_visualizer_and_readonly_dashboard(tmp_path, monkeypatch):
    store_path = tmp_path / 'requests.db'
    store = Store(store_path)
    req = _create_req(store)
    ctx = FakeContext('default', store_path)
    host = Host(ctx, store=store)
    host.goals.spawn_goal({
        'goal_id': 'g_dash', 'request_ref': {'id': req['id'], 'generation': 1},
        'objective': 'Dashboard test', 'done_criteria': 'Done',
        'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
        'budget': {'limits': {'gpu.alaya': 1, 'wall_hours': 2}, 'constraints': ['Low priority']},
    })
    host.scan()
    packet = host.wake_packet()
    assert packet['active_goals']['count'] == 1
    assert 'active_usage' in packet['resources']

    assert 'g_dash' in render_text_tree(store_path)
    html = render_html(store_path)
    assert 'Low priority' in html
    assert 'id="ancor-data"' in html
    assert 'id="treeContainer"' in html
    assert 'id="detailPane"' in html

    monkeypatch.setenv('ANCOR_STORE_PATH', str(store_path))
    app = FastAPI()
    app.include_router(dashboard_router, prefix='/api/plugins/ancor')
    client = TestClient(app)
    ov = client.get('/api/plugins/ancor/overview').json()
    assert ov['goals']['count'] == 1
    assert ov['requests']['count'] == 1
    g_detail = client.get('/api/plugins/ancor/goals/g_dash').json()
    assert g_detail['goal']['goal_id'] == 'g_dash'


def test_shared_gateway_dispatches_by_session_identity_not_first_role(tmp_path, monkeypatch):
    from ancor.session_identity import launch_identity
    store = Store(tmp_path / 'requests.db')
    from test_goals import _make_request, _repair_spec
    req = _make_request(store)
    goals = GoalStore(store)
    goals.spawn_goal(_repair_spec(req, 'first'))
    goals.spawn_goal(_repair_spec(req, 'second'))
    ctx = FakeContext('ancor-exec', store.path)
    ctx._manager = object()  # Native PluginContext is shared across sessions.
    monkeypatch.setenv('HERMES_EPHEMERAL_SYSTEM_PROMPT', launch_identity('root'))
    ancor.register(ctx)
    assert json.loads(ctx.tools['ancor_goal_self']['handler']({}))['code'] == 'forbidden'
    for gid in ('first', 'second'):
        monkeypatch.setenv('HERMES_EPHEMERAL_SYSTEM_PROMPT', launch_identity('goal', gid))
        result = json.loads(ctx.tools['ancor_goal_self']['handler']({}))
        assert result['goal']['goal_id'] == gid
        assert json.loads(ctx.tools['ancor_delivery_submit']['handler']({}))['code'] == 'forbidden'
        ctx.hooks['post_api_request'](model='same-model', provider='custom')
        assert goals.get_goal(gid)['observed_models'] == [{'model': 'same-model', 'provider': 'custom'}]
    monkeypatch.setenv('HERMES_EPHEMERAL_SYSTEM_PROMPT', launch_identity('judge'))
    assert json.loads(ctx.tools['ancor_goal_self']['handler']({}))['code'] == 'forbidden'
    assert ctx.hooks['pre_tool_call']('terminal', {})['action'] == 'block'


def test_host_keeps_scanning_while_goal_root_and_judge_are_running(tmp_path, monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace
    import ancor.host as host_module
    from ancor.watcher import GoalRunner
    from test_goals import _make_request, _repair_spec
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store)
    goals.spawn_goal(_repair_spec(req, 'long_goal'))
    runner = GoalRunner(goals, command_builder=lambda *a, **kw: [sys.executable, '-c', 'import time; time.sleep(60)'])
    adapter = SimpleNamespace(is_connected=True)
    seen = {'scans': 0, 'root': False, 'judge': False, 'live_during_scan': False}
    async def root():
        seen['root'] = True
        await asyncio.Event().wait()
    async def judge():
        seen['judge'] = True
        await asyncio.Event().wait()
    def scan():
        seen['scans'] += 1
        if seen['scans'] > 1 and goals.get_goal('long_goal')['pid']:
            seen['live_during_scan'] = True
        if seen['scans'] >= 3:
            adapter.is_connected = False
    def no_notifications():
        raise ValueError('isolated test')
    fake = SimpleNamespace(scan=scan, runner=runner, dispatch_judges=judge,
                           coordinator=SimpleNamespace(tick=root), notification_dispatcher=no_notifications)
    monkeypatch.setattr(host_module, 'Host', lambda ctx: fake)
    sleep = asyncio.sleep
    async def fast_sleep(delay):
        await sleep(.05 if delay == 5 else delay)
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
    monkeypatch.delenv('ANCOR_ROLE', raising=False)
    monkeypatch.delenv('GOAL_ID', raising=False)
    asyncio.run(asyncio.wait_for(host_module.scan_runner(
        FakeContext('default', store.path, poll_interval=5), adapter), 5))
    assert seen == {'scans': 3, 'root': True, 'judge': True, 'live_during_scan': True}
    assert not runner.tasks and goals.get_goal('long_goal')['pid'] is None


def test_request_transition_waits_for_confirmed_stop(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _create_req(store)
    goals = GoalStore(store)
    from test_goals import _repair_spec
    goals.spawn_goal(_repair_spec(req, 'pending'))
    goals.set_process('pending', 98765)
    proposal = store.propose_change(req['id'], [{'op': 'cancel', 'reason': 'stop'}],
        actor='default', source={'kind': 'interactor_proposal', 'input': 'stop'},
        base={'generation': req['generation'], 'control_version': req['control_version']})
    store.commit_change(proposal['id'], 'cancel-confirm')
    result = advance_request(store, goals, req['id'], process_killer=lambda _: False)
    assert result['outcome'] == 'blocked'
    assert result['target_status'] == 'cancelled' and result['status'] == 'active'
    assert goals.get_goal('pending')['pid'] == 98765
    result = advance_request(store, GoalStore(Store(store.path)), req['id'], process_killer=lambda _: True)
    assert result['outcome'] == 'completed' and result['status'] == 'cancelled'


def test_stalled_active_request_warns_on_ack_and_emits_stalled_event_with_cooldown(tmp_path, monkeypatch):
    from ancor.host import Host, STALLED_COOLDOWN_SECONDS
    now = [10000.0]
    store = Store(tmp_path / 'requests.db')
    req = _create_req(store)
    ctx_root = FakeContext('ancor-exec', store.path)
    monkeypatch.setenv('ANCOR_ROLE', 'root')
    monkeypatch.delenv('GOAL_ID', raising=False)
    ancor.register(ctx_root)

    # Ack initial request.created event without spawning any goal -> immediate warning
    pending_ids = [e['id'] for e in store.pending_events()]
    ack_res = json.loads(ctx_root.tools['ancor_event_ack']['handler']({'event_ids': pending_ids}))
    assert ack_res['acked'] == pending_ids
    assert len(ack_res.get('stalled_requests', [])) == 1
    assert ack_res['stalled_requests'][0]['request_id'] == req['id']

    # Host.scan() emits request.stalled once and respects cooldown
    host = Host(FakeContext('default', store.path), store=store, clock=lambda: now[0])
    host.scan()
    assert host.last_stalled == [req['id']]
    stalled_events = [e for e in store.pending_events() if e['kind'] == 'request.stalled']
    assert len(stalled_events) == 1

    # Ack stalled event without spawning -> within cooldown, scan does not spam
    store.ack_events([e['id'] for e in stalled_events])
    now[0] += 60.0
    host.scan()
    assert host.last_stalled == []

    # After cooldown expires, scan re-emits request.stalled
    now[0] += STALLED_COOLDOWN_SECONDS
    host.scan()
    assert host.last_stalled == [req['id']]
