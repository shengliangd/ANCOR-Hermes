#!/usr/bin/env python3
"""Isolated-instance end-to-end manual test suite with toy examples (Plan A + generic resources + visualizer)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import ancor
from ancor.dashboard.plugin_api import router as dashboard_router
from ancor.host import Host
from ancor.store import Store


class IsolatedContext:
    def __init__(self, profile_name: str, store_path: Path, cluster_path: Path, **extra):
        self.profile_name = profile_name
        self.config = {
            'store_path': str(store_path),
            'cluster_state_path': str(cluster_path),
            'interactor_profile': 'default',
            'exec_profile': 'ancor-exec',
            'owner_profile': 'default',
            'gateway_owner': profile_name == 'default',
            'human_target': 'toy:human',
            'global_limits': {'gpu.alaya': 6, 'api_usd': 100},
            'max_judge_rejects': 2,
            **extra,
        }
        self.tools = {}
        self.hooks = {}
        self.platform_handlers = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, *, name, toolset, schema, handler):
        self.tools[name] = {'toolset': toolset, 'schema': schema, 'handler': handler}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_platform_handler(self, name, fn):
        self.platform_handlers[name] = fn


def call_tool(ctx: IsolatedContext, name: str, args: dict) -> dict:
    return json.loads(ctx.tools[name]['handler'](args))


def confirm_proposal(interactor: IsolatedContext, proposal: dict, call_id: str) -> None:
    clarify_args = proposal['clarify_args']
    question = clarify_args['questions'][0]
    confirm_choice = question['choices'][1]
    pre = interactor.hooks['pre_tool_call'](
        'clarify', clarify_args, session_id='toy-session', tool_call_id=call_id)
    assert pre is None, f'pre_tool_call blocked: {pre}'
    interactor.hooks['post_tool_call'](
        'clarify', clarify_args,
        {'outcome': 'submitted', 'responses': [{
            'question': question['question'],
            'choices_offered': question['choices'],
            'status': 'answered',
            'user_response': confirm_choice,
        }]},
        session_id='toy-session', tool_call_id=call_id, status='ok')


def run_toy_suite(root: Path) -> dict:
    hermes_live = (Path.home() / '.hermes').resolve()
    assert not root.resolve().is_relative_to(hermes_live), 'Refusing to run inside live ~/.hermes!'

    store_path = root / 'ancor' / 'requests.db'
    cluster_path = root / 'ancor' / 'cluster.json'
    workspace = root / 'workspace'
    (root / 'ancor').mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    exec_config = root / 'profiles' / 'ancor-exec' / 'config.yaml'
    exec_config.parent.mkdir(parents=True)
    exec_config.write_text(yaml.safe_dump({
        'model_aliases': {name: {'model': 'toy-' + name, 'provider': 'custom'}
                          for name in ('root', 'research', 'fast', 'coding', 'judge')},
    }))

    # Plan A: only 2 profiles (default = Interactor + Gateway Owner; ancor-exec + ANCOR_ROLE = root/goal/judge)
    os.environ.pop('ANCOR_ROLE', None)
    os.environ.pop('GOAL_ID', None)
    def context(profile):
        return IsolatedContext(profile, store_path, cluster_path,
                               exec_config_path=str(exec_config))

    interactor = context('default')
    ancor.register(interactor)

    os.environ['ANCOR_ROLE'] = 'root'
    root_agent = context('ancor-exec')
    ancor.register(root_agent)
    assert root_agent.hooks['pre_tool_call']('terminal')['action'] == 'block'

    os.environ['ANCOR_ROLE'] = 'goal'
    goal_agent = context('ancor-exec')
    ancor.register(goal_agent)
    assert goal_agent.hooks['pre_tool_call']('clarify')['action'] == 'block'

    os.environ['ANCOR_ROLE'] = 'judge'
    judge = context('ancor-exec')
    ancor.register(judge)
    assert judge.hooks['pre_tool_call']('write_file')['action'] == 'block'
    os.environ.pop('ANCOR_ROLE', None)

    store = Store(store_path)
    host = Host(interactor, store=store)
    results = {}

    # 1. Happy path with generic 3-layer budget (limits + allowed + natural-language constraints)
    prop = call_tool(interactor, 'ancor_change_propose', {
        'input': 'Run toy Fibonacci benchmark within budget and verify checksum',
        'operations': [{'op': 'create', 'title': 'Toy Fibonacci Benchmark', 'fields': {
            'outcome': ['Produce benchmark_results.json with fib(20)=6765'],
            'constraint': ['Stay within allocated alaya GPUs and USD budget'],
        }}],
    })
    confirm_proposal(interactor, prop, 'call-create-1')
    rid1 = prop['request_id']

    toy1_dir = workspace / 'toy1'
    toy1_dir.mkdir()
    l1 = call_tool(root_agent, 'ancor_goal_spawn', {'goal_yaml': f"""
goal_id: g_toy1_l1
parent_goal_id: root
request_ref:
  id: "{rid1}"
  generation: 1
objective: "Execute toy Fibonacci benchmark and verify results"
done_criteria: "benchmark_results.json written with fib(20)=6765"
category: research
scope:
  readable_requests: ["{rid1}"]
  can_ask_human: false
budget:
  limits:
    gpu.alaya: 3
    api_usd: 15
    wall_hours: 4
  allowed:
    category: [research, fast]
    clusters: ["alaya"]
    writable_paths: ["{toy1_dir}"]
  constraints:
    - "Prefer off-peak hours for heavy runs"
"""})
    assert l1['goal_id'] == 'g_toy1_l1'

    os.environ['GOAL_ID'] = 'g_toy1_l1'
    l2_a = call_tool(goal_agent, 'ancor_goal_spawn', {
        'goal_id': 'g_toy1_l2_compute',
        'request_ref': {'id': rid1, 'generation': 1},
        'objective': 'Compute fib(20) on alaya',
        'done_criteria': 'JSON saved',
        'category': 'fast',
        'scope': {'readable_requests': [rid1], 'can_ask_human': False},
        'budget': {
            'limits': {'gpu.alaya': 2, 'api_usd': 10, 'wall_hours': 2},
            'allowed': {'writable_paths': [str(toy1_dir / 'compute')]},
            'constraints': ['Check local cache first'],
        },
    })
    assert len(l2_a['budget']['constraints']) == 2  # inherited + appended
    assert l2_a['category'] == 'fast'

    # Tool boundary: reject unknown aliases and categories outside the parent's budget.
    for category in ('typo', 'coding'):
        rejected = call_tool(goal_agent, 'ancor_goal_spawn', {
            'goal_id': 'g_reject_' + category,
            'request_ref': {'id': rid1, 'generation': 1},
            'objective': 'Must be rejected', 'done_criteria': 'Never',
            'category': category,
            'scope': {'readable_requests': [rid1], 'can_ask_human': False},
        })
        assert rejected.get('error'), rejected
        assert rejected['code'] == ('invalid_request' if category == 'typo' else 'attenuation_violation')

    over_usd = call_tool(goal_agent, 'ancor_goal_spawn', {
        'goal_id': 'g_toy1_l2_over_usd',
        'request_ref': {'id': rid1, 'generation': 1},
        'objective': 'Exceed api_usd',
        'done_criteria': 'Never',
        'scope': {'readable_requests': [rid1], 'can_ask_human': False},
        'budget': {'limits': {'gpu.alaya': 1, 'api_usd': 10, 'wall_hours': 1}},
    })
    assert over_usd['code'] == 'attenuation_violation'

    # Real subprocess launched by GoalRunner under ancor-exec with ANCOR_ROLE=goal + GOAL_ID
    artifact_path = toy1_dir / 'compute' / 'benchmark_results.json'
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    worker_script = root / 'toy_worker.py'
    worker_script.write_text(f"""
import json, os, pathlib, subprocess, sys
assert os.environ.get('ANCOR_ROLE') == 'goal'
def fib(n):
    a, b = 0, 1
    for _ in range(n): a, b = b, a + b
    return a
r1 = subprocess.run([sys.executable, '-m', 'ancor.quota_guard',
    '--store', {str(store_path)!r}, '--use', 'gpu.alaya=2', '--use', 'api_usd=4.5',
    '--tag', 'clusters=alaya', '--handle-id', 'toy_h1'],
    env={{**os.environ, 'PYTHONPATH': {str(REPO)!r}}}, capture_output=True, text=True)
assert r1.returncode == 0, r1.stderr
r2 = subprocess.run([sys.executable, '-m', 'ancor.quota_guard',
    '--store', {str(store_path)!r}, '--use', 'api_usd=6', '--handle-id', 'toy_h_over'],
    env={{**os.environ, 'PYTHONPATH': {str(REPO)!r}}}, capture_output=True, text=True)
assert r2.returncode == 2
pathlib.Path({str(artifact_path)!r}).write_text(json.dumps({{'fib_20': fib(20), 'goal_id': os.environ['GOAL_ID']}}))
""")
    def toy_command(profile, sid, prompt, *, resume, model_args=()):
        assert profile == 'ancor-exec' and list(model_args) == ['-m', 'fast']
        packet = json.loads(prompt.split('\n', 1)[1])
        assert packet['category'] == 'fast' and 'research' in packet['model_categories']
        return [sys.executable, str(worker_script)]

    host.runner.command_builder = toy_command
    assert host.runner.run_goal_sync('g_toy1_l2_compute') == 0
    assert json.loads(artifact_path.read_text())['fib_20'] == 6765

    # Watcher transition -> GoalResult -> Judge accept -> Human accept
    host.cluster.submit_handle(handle_id='toy_h1', usage={'gpu.alaya': 2, 'api_usd': 4.5},
                               tags={'clusters': 'alaya'}, goal_id='g_toy1_l2_compute')
    host.goals.mark_waiting_external('g_toy1_l2_compute')
    assert host.watcher.poll()['woken_goals'] == []
    host.cluster.set_status('toy_h1', 'succeeded', detail='fib(20)=6765')
    assert host.watcher.poll()['woken_goals'] == ['g_toy1_l2_compute']

    os.environ['GOAL_ID'] = 'g_toy1_l2_compute'
    goal_agent.hooks['post_api_request'](model='toy-fast', provider='custom')
    goal_agent.hooks['post_api_request'](model='toy-fallback', provider='custom')
    call_tool(goal_agent, 'ancor_goal_report', {
        'request_generation': 1, 'status': 'completed', 'summary': 'Computed fib(20)=6765',
        'artifacts': [{'path': str(artifact_path), 'desc': 'output'}], 'metrics': {'fib_20': 6765}, 'open_issues': [],
    })
    os.environ['GOAL_ID'] = 'g_toy1_l1'
    call_tool(goal_agent, 'ancor_goal_report', {
        'request_generation': 1, 'status': 'completed', 'summary': 'All done',
        'artifacts': [{'path': str(artifact_path), 'desc': 'output'}], 'metrics': {'fib_20': 6765}, 'open_issues': [],
    })
    os.environ.pop('GOAL_ID', None)

    call_tool(root_agent, 'ancor_delivery_submit', {
        'request_id': rid1, 'summary': 'fib(20)=6765 complete',
        'artifacts': [{'path': str(artifact_path), 'desc': 'benchmark_results.json'}],
    })
    j_req = call_tool(judge, 'ancor_request_read', {'request_id': rid1})
    cov = [{'version_id': p['version_id'], 'satisfied': True, 'evidence': 'Verified'}
           for f in ('outcome', 'constraint') for p in j_req['content']['fields'][f]]
    verdict = call_tool(judge, 'ancor_judge_verdict', {
        'request_id': rid1, 'generation': 1, 'decision': 'accept', 'summary': 'Verified cleanly',
        'evidence': {'generation': 1, 'coverage': cov, 'verification': ['Checked JSON'], 'artifacts': [str(artifact_path)]},
    })
    assert verdict['request_status'] == 'awaiting_acceptance'
    accept_prop = call_tool(interactor, 'ancor_request_accept_propose', {'request_id': rid1})
    confirm_proposal(interactor, accept_prop, 'call-accept-1')
    assert store.get_request(rid1)['status'] == 'accepted'
    results['toy1_plan_a_generic_resources'] = 'PASS'
    saved = host.goals.get_goal('g_toy1_l2_compute')
    assert saved['category'] == 'fast'
    assert [m['model'] for m in saved['observed_models']] == ['toy-fast', 'toy-fallback']
    assert saved['result']['observed_models'] == saved['observed_models']
    assert saved['result']['category'] == 'fast'
    results['toy3_category_routing_and_observability'] = 'PASS'

    # 2. Read-only DB visualizer + Hermes Desktop Dashboard API
    viz_html = root / 'ancor_view.html'
    viz = call_tool(interactor, 'ancor_visualize', {'output_path': str(viz_html)})
    assert 'g_toy1_l2_compute' in viz['tree']
    assert 'category=fast' in viz['tree'] and 'toy-fallback' in viz['tree']
    assert viz_html.exists() and 'Prefer off-peak hours for heavy runs' in viz_html.read_text(encoding='utf-8')

    os.environ['ANCOR_STORE_PATH'] = str(store_path)
    app = FastAPI()
    app.include_router(dashboard_router, prefix='/api/plugins/ancor')
    client = TestClient(app)
    ov = client.get('/api/plugins/ancor/overview').json()
    assert ov['requests']['count'] == 1 and ov['goals']['count'] == 2
    results['toy2_visualizer_and_desktop_api'] = 'PASS'

    return {
        'isolated_root': str(root),
        'profiles_used': ['default', 'ancor-exec'],
        'live_hermes_untouched': True,
        'scenarios': results,
    }


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix='ancor-isolated-toy-'))
    try:
        print(json.dumps(run_toy_suite(root), ensure_ascii=False, indent=2))
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
