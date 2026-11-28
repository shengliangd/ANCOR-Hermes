"""Tests for Shift-Based Ephemeral Goal Execution (handoff ledger, deterministic rotation, anti-loop, MCP & Dashboard)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import stat
import sys

from fastapi.testclient import TestClient
import pytest

from ancor.backends import BackendRouter
from ancor.cli.main import main as cli_main
from ancor.config import load_config
from ancor.core_api import AncorCore
from ancor.goals import HANDOFF_MAX_BYTES, validate_checklist, validate_handoff
from ancor.mcp_server import build_role_tools, execute_role_tool
from ancor.serve_app import create_serve_app
from ancor.watcher import GoalRunner, SessionRotationPolicy, build_goal_prompt, decide_session_reuse


def test_decide_session_reuse_all_rotation_triggers():
    policy = SessionRotationPolicy(
        session_reuse_ttl_seconds=300.0,
        max_wakes_per_session=3,
        max_shifts_per_goal=20,
    )
    base_goal = {
        'session_id': 'sess-1',
        'backend': 'codex',
        'session_wakes': 1,
        'last_run_finished_at': 1000.0,
        'handoff_requested': False,
    }

    # Normal short wake -> reuse
    assert decide_session_reuse(base_goal, [], policy, 1100.0, selected_backend='codex') == (True, None)
    # 1. first_run
    assert decide_session_reuse({**base_goal, 'session_id': ''}, [], policy, 1100.0) == (False, 'first_run')
    # 2. agent_handoff
    assert decide_session_reuse({**base_goal, 'handoff_requested': True}, [], policy, 1100.0) == (False, 'agent_handoff')
    # 3. steered / generation_changed
    assert decide_session_reuse(base_goal, [{'kind': 'steer', 'payload': {}}], policy, 1100.0) == (False, 'steered')
    assert decide_session_reuse(base_goal, [{'kind': 'update_generation', 'payload': {}}], policy, 1100.0) == (False, 'generation_changed')
    # 4. ttl_expired
    assert decide_session_reuse(base_goal, [], policy, 1301.0) == (False, 'ttl_expired')
    # 5. max_wakes
    assert decide_session_reuse({**base_goal, 'session_wakes': 3}, [], policy, 1100.0) == (False, 'max_wakes')
    # 6. backend_fallback
    assert decide_session_reuse(base_goal, [], policy, 1100.0, selected_backend='hermes') == (False, 'backend_fallback')
    # 7. recovery
    assert decide_session_reuse(base_goal, [{'kind': 'recovery', 'payload': {}}], policy, 1100.0) == (False, 'recovery')


def test_handoff_size_limit_and_checklist_validation():
    h = validate_handoff({
        'verified_progress': ['step 1 passed'],
        'dead_ends': ['do not use /tmp'],
        'next_step': 'run eval on full dev set',
        'artifact_refs': ['/work/out.json'],
    }, now=100.0, shift_seq=2)
    assert h['shift_seq'] == 2
    assert h['dead_ends'] == ['do not use /tmp']

    with pytest.raises(ValueError, match=f'{HANDOFF_MAX_BYTES} byte limit'):
        validate_handoff({
            'verified_progress': ['x' * 3100],
            'next_step': 'too big',
        }, now=100.0)

    cl = validate_checklist([
        {'id': 'c1', 'item': 'Prepare data', 'status': 'done', 'evidence': '12 files clean'},
        {'id': 'c2', 'item': 'Run eval', 'status': 'pending', 'verify_hint': 'check accuracy > 0.9'},
    ])
    assert len(cl) == 2 and cl[0]['status'] == 'done'

    with pytest.raises(ValueError, match='duplicate checklist item id'):
        validate_checklist([
            {'id': 'c1', 'item': 'a', 'status': 'pending'},
            {'id': 'c1', 'item': 'b', 'status': 'done'},
        ])


def test_shift_handoff_wait_prompt_dashboard_and_anti_loop(tmp_path, capsys):
    repo_root = str(Path(__file__).resolve().parents[1])
    db = tmp_path / 'requests.db'
    cfg_path = tmp_path / 'config.yaml'

    # Fake backend script: shift 1 does handoff, shift 2 verifies prompt has handoff & completes.
    fake_cli = tmp_path / 'fake_shift_cli.py'
    seen_prompts = tmp_path / 'seen_prompts.jsonl'
    fake_cli.write_text(
        f'''#!{sys.executable}
import json, os, sys
sys.path.insert(0, {repo_root!r})
from ancor.config import load_config
from ancor.core_api import AncorCore

prompt = sys.argv[-1]
with open({str(seen_prompts)!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:-1], "prompt": prompt}}) + "\\n")

core = AncorCore(load_config(store_path_override=os.environ["ANCOR_STORE_PATH"]))
gid = os.environ["GOAL_ID"]
goal = core.goal_store.get_goal(gid)
if goal.get("handoff") is None:
    print(json.dumps({{"type": "thread.started", "thread_id": "sess-shift-1"}}), flush=True)
    core.goal_handoff(
        gid,
        handoff={{
            "verified_progress": ["dataset validated"],
            "dead_ends": ["single-thread ossfs too slow"],
            "next_step": "run parallel stage",
            "artifact_refs": ["/tmp/stage1.json"],
        }},
        checklist=[
            {{"id": "s1", "item": "validate", "status": "done"}},
            {{"id": "s2", "item": "train", "status": "pending"}},
        ],
    )
else:
    print(json.dumps({{"type": "thread.started", "thread_id": "sess-shift-2"}}), flush=True)
    core.goal_report(gid, {{
        "request_generation": 1,
        "status": "completed",
        "summary": "finished shift 2 after handoff",
    }})
''',
        encoding='utf-8',
    )
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IEXEC)

    cfg_path.write_text(
        f'''
store_path: {db}
default_backend: codex
session_reuse_ttl_seconds: 300
max_wakes_per_session: 3
max_shifts_per_goal: 20
backends:
  codex:
    adapter: codex
    command: {fake_cli}
categories:
  coding:
    backend: codex
    args: []
''',
        encoding='utf-8',
    )
    cfg = load_config(cfg_path)
    core = AncorCore(cfg)
    req_id = core.request_create(title='Shift E2E', fields={'outcome': ['complete in 2 shifts']})['request']['id']
    goal = core.goal_spawn({
        'request_ref': {'id': req_id, 'generation': 1},
        'objective': 'Multi-shift work',
        'done_criteria': 'completed',
        'category': 'coding',
    })
    gid = goal['goal_id']

    router = BackendRouter(cfg)
    runner = GoalRunner(
        core.goal_store,
        backend_router=router,
        rotation_policy=SessionRotationPolicy(300.0, 3, 20),
    )

    # Shift 1: fresh start -> calls handoff -> discards session_id & schedules wake
    assert asyncio.run(runner.run_goal(gid)) == 0
    mid = core.goal_store.get_goal(gid)
    assert mid['status'] == 'running'
    assert mid['needs_wake'] is True
    assert mid['session_id'] == ''
    assert mid['handoff_requested'] is True
    assert mid['handoff']['next_step'] == 'run parallel stage'

    # Shift 2: rotates cleanly with rotation_reason='agent_handoff' & completes
    assert asyncio.run(runner.run_goal(gid)) == 0
    done = core.goal_store.get_goal(gid)
    assert done['status'] == 'completed'
    assert done['session_id'] == 'sess-shift-2'

    # Check shift audit ledger
    shifts = core.goal_store.list_shifts(gid)
    assert len(shifts) == 2
    assert shifts[0]['shift_seq'] == 1 and shifts[0]['rotation_reason'] == 'first_run' and shifts[0]['outcome'] == 'handoff'
    assert shifts[1]['shift_seq'] == 2 and shifts[1]['rotation_reason'] == 'agent_handoff' and shifts[1]['outcome'] == 'completed'
    assert shifts[1]['reused_session'] is False

    # Verify shift 2 prompt received handoff, checklist, shift_seq, rotation_reason
    prompt_logs = [json.loads(line) for line in seen_prompts.read_text(encoding='utf-8').splitlines()]
    assert len(prompt_logs) == 2
    assert 'resume' not in prompt_logs[1]['argv']
    assert 'run parallel stage' in prompt_logs[1]['prompt']
    assert 'single-thread ossfs too slow' in prompt_logs[1]['prompt']
    assert '"shift_seq":2' in prompt_logs[1]['prompt']
    assert '"rotation_reason":"agent_handoff"' in prompt_logs[1]['prompt']

    # Dashboard /goals/{goal_id} returns handoff, checklist, and shifts
    app = create_serve_app(cfg)
    client = TestClient(app)
    resp = client.get(f'/goals/{gid}')
    assert resp.status_code == 200
    body = resp.json()
    assert body['handoff']['next_step'] == 'run parallel stage'
    assert len(body['checklist']) == 2
    assert len(body['shifts']) == 2

    # CLI `ancor core goal shifts`
    rc = cli_main(['--db', str(db), 'core', 'goal', 'shifts', gid])
    assert rc == 0
    cli_shifts = json.loads(capsys.readouterr().out)
    assert cli_shifts['count'] == 2


def test_wait_with_handoff_mcp_and_stagnant_handoff_anti_loop(tmp_path):
    cfg = load_config(store_path_override=tmp_path / 'requests.db')
    core = AncorCore(cfg)
    req_id = core.request_create(title='Guard Test', fields={'outcome': ['test guards']})['request']['id']
    gid = core.goal_spawn({
        'request_ref': {'id': req_id, 'generation': 1},
        'objective': 'Test wait & handoff guards',
        'done_criteria': 'done',
    })['goal_id']

    # MCP ancor_goal_wait persists handoff & checklist atomically
    goal_defs, goal_handlers = build_role_tools(core, role='goal', goal_id=gid)
    assert 'ancor_goal_handoff' in goal_defs
    wres = json.loads(execute_role_tool(goal_defs, goal_handlers, 'ancor_goal_wait', {
        'wake_after_seconds': 60,
        'reason': 'waiting for job',
        'handoff': {'verified_progress': ['job submitted'], 'next_step': 'check job'},
        'checklist': [{'id': 'j1', 'item': 'submit', 'status': 'done'}],
    }))
    assert wres['status'] == 'waiting_external'
    assert wres['handoff']['next_step'] == 'check job'

    # Anti-loop guard: 3 consecutive stagnant agent_handoffs transition goal to blocked
    gid2 = core.goal_spawn({
        'request_ref': {'id': req_id, 'generation': 1},
        'objective': 'Looping goal',
        'done_criteria': 'done',
    })['goal_id']
    same_handoff = {'verified_progress': ['stuck at step 1'], 'next_step': 'retry step 1'}
    # Shift 1 -> handoff #1
    s1 = core.goal_store.start_shift(gid2, backend='codex', backend_args=[], session_id='', reused_session=False, rotation_reason='first_run')
    core.goal_store.handoff_goal(gid2, handoff=same_handoff)
    core.goal_store.finish_shift(s1['id'], goal_id=gid2, exit_code=0, outcome='handoff')
    # Shift 2 -> handoff #2 (same verified_progress)
    s2 = core.goal_store.start_shift(gid2, backend='codex', backend_args=[], session_id='', reused_session=False, rotation_reason='agent_handoff')
    core.goal_store.handoff_goal(gid2, handoff=same_handoff)
    core.goal_store.finish_shift(s2['id'], goal_id=gid2, exit_code=0, outcome='handoff')
    # Shift 3 -> handoff #3 (stagnant!) -> blocked deterministically
    s3 = core.goal_store.start_shift(gid2, backend='codex', backend_args=[], session_id='', reused_session=False, rotation_reason='agent_handoff')
    guard_res = core.goal_store.handoff_goal(gid2, handoff=same_handoff)
    assert guard_res['status'] == 'blocked'
    assert guard_res['blocked_by_guard'] == 'stagnant_handoff_loop'
    assert core.goal_store.get_goal(gid2)['status'] == 'blocked'

    # Anti-loop guard: max_shifts_per_goal
    gid3 = core.goal_spawn({
        'request_ref': {'id': req_id, 'generation': 1},
        'objective': 'Max shifts goal',
        'done_criteria': 'done',
    })['goal_id']
    for i in range(2):
        sh = core.goal_store.start_shift(gid3, backend='codex', backend_args=[], session_id='', reused_session=False, rotation_reason='first_run' if i == 0 else 'max_wakes')
        core.goal_store.finish_shift(sh['id'], goal_id=gid3, exit_code=0, outcome='running')
    blocked = core.goal_store.enforce_shift_limit(gid3, max_shifts_per_goal=2)
    assert blocked is not None and blocked['status'] == 'blocked'
