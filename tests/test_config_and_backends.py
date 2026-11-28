"""Tests for standalone config, ExecutionBackend adapters, BackendRouter, and multi-backend GoalRunner."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import stat
import sys

import pytest

from ancor.backends import (
    AgyBackend,
    BackendCapabilities,
    BackendRegistry,
    BackendRouter,
    CodexBackend,
    DshBackend,
    ExecutionBackend,
    HermesBackend,
    LaunchSpec,
)
from ancor.config import load_config
from ancor.core_api import AncorCore
from ancor.watcher import GoalRunner


class _SoftOnlyBackend(ExecutionBackend):
    capabilities = BackendCapabilities(
        supports_mcp=True,
        supports_resume=False,
        supports_readonly_fence=False,
        supports_model_telemetry=False,
    )

    def build_command(self, spec: LaunchSpec):
        return [self.command, *self.default_args, *spec.extra_args, spec.prompt], self._base_env(spec), []


def _write_config(tmp_path: Path, extra_yaml: str = '') -> Path:
    db = tmp_path / 'requests.db'
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(
        f'''
store_path: {db}
default_backend: hermes
backends:
  hermes:
    adapter: hermes
    command: hermes
    default_args: ["-p", "ancor-exec"]
  codex:
    adapter: codex
    command: codex
categories:
  root:
    backend: hermes
    args: ["-m", "root"]
  judge:
    backend: codex
    args: ["-m", "gpt-5.3-codex"]
    fallbacks:
      - backend: hermes
        args: ["-m", "judge"]
  coding:
    backend: codex
    args: ["-m", "gpt-5.3-codex", "-c", "model_reasoning_effort=high"]
    fallbacks:
      - backend: hermes
        args: ["-m", "coding"]
{extra_yaml}
''',
        encoding='utf-8',
    )
    return cfg


def test_config_loads_backends_categories_and_notifiers(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_HOOK_TOKEN', 'secret-123')
    cfg_path = _write_config(
        tmp_path,
        '''
notifiers:
  - name: cli-notify
    type: command
    command: echo
    args: ["{kind}:{message}"]
    input_mode: template_args
    kinds: ["question", "completion"]
  - name: hook
    type: webhook
    url: https://example.invalid/hook
    headers:
      Authorization: "Bearer ${TEST_HOOK_TOKEN}"
    kinds: ["completion"]
''',
    )
    cfg = load_config(cfg_path)
    assert cfg.store_path == (tmp_path / 'requests.db').resolve()
    assert set(cfg.backends) == {'hermes', 'codex'}
    assert cfg.categories['coding'].backend == 'codex'
    assert cfg.categories['coding'].args == ['-m', 'gpt-5.3-codex', '-c', 'model_reasoning_effort=high']
    assert len(cfg.categories['coding'].fallbacks) == 1
    assert cfg.categories['coding'].fallbacks[0].backend == 'hermes'
    assert len(cfg.notifiers) == 2
    assert cfg.notifiers[1].headers['Authorization'] == 'Bearer secret-123'


def test_codex_backend_command_mcp_injection_and_readonly_fence():
    be = CodexBackend(name='codex', command='codex')
    mcp_cmd = [sys.executable, '-m', 'ancor.cli.main', 'mcp', '--role', 'judge']
    cmd, env, _ = be.build_command(LaunchSpec(
        role='judge',
        prompt='verify delivery',
        system_prompt='You are judge',
        extra_args=['-m', 'gpt-5.3-codex'],
        mcp_command=mcp_cmd,
    ))
    assert cmd[:4] == ['codex', 'exec', '--json', '--skip-git-repo-check']
    assert '-s' in cmd and cmd[cmd.index('-s') + 1] == 'read-only'
    assert '--dangerously-bypass-approvals-and-sandbox' not in cmd
    assert any(arg.startswith('mcp_servers.ancor.command=') for arg in cmd)
    assert any(arg.startswith('mcp_servers.ancor.args=') for arg in cmd)
    assert env['ANCOR_ROLE'] == 'judge'

    # Resume mode uses `codex exec resume ... <session_id> <prompt>`
    rcmd, _, _ = be.build_command(LaunchSpec(
        role='goal',
        goal_id='g1',
        session_id='thread-abc',
        prompt='continue work',
        mcp_command=mcp_cmd,
    ))
    assert rcmd[:3] == ['codex', 'exec', 'resume']
    assert rcmd[-2:] == ['thread-abc', 'continue work']

    ev = be.parse_output_line(json.dumps({'type': 'thread.started', 'thread_id': 'thread-xyz'}))
    assert ev is not None and ev.kind == 'session' and ev.session_id == 'thread-xyz'


def test_hermes_backend_command_and_output_parsing():
    be = HermesBackend(name='hermes', command='hermes', default_args=['-p', 'ancor-exec'])
    cmd, env, _ = be.build_command(LaunchSpec(
        role='goal',
        goal_id='g-1',
        session_title='ancor-req1-g-1',
        prompt='do task',
        extra_args=['-m', 'research'],
    ))
    assert cmd[:5] == ['hermes', '-p', 'ancor-exec', '--cli', 'chat']
    assert '-m' in cmd and 'research' in cmd
    assert '-c' in cmd and 'ancor-req1-g-1' in cmd
    assert env['ANCOR_ROLE'] == 'goal'
    assert env['GOAL_ID'] == 'g-1'
    assert 'HERMES_EPHEMERAL_SYSTEM_PROMPT' in env

    ev1 = be.parse_output_line('Session: sess-123', is_stderr=True)
    assert ev1 is not None and ev1.session_id == 'sess-123'
    ev2 = be.parse_output_line(json.dumps({'type': 'system', 'session_id': 'sess-456'}))
    assert ev2 is not None and ev2.session_id == 'sess-456'


def test_backend_router_cooldown_fallback_and_role_policy(tmp_path):
    cfg = load_config(_write_config(tmp_path))
    now = [100.0]
    router = BackendRouter(cfg, clock=lambda: now[0])

    # Primary route for coding -> codex
    r1 = router.resolve(role='goal', category='coding')
    assert r1.backend.name == 'codex'
    assert r1.fallback_used is False

    # Cooldown codex -> falls back to hermes
    router.mark_backend_cooldown('codex', seconds=60.0)
    r2 = router.resolve(role='goal', category='coding')
    assert r2.backend.name == 'hermes'
    assert r2.fallback_used is True

    # Bound backend locks resume to codex even while codex is in cooldown
    r3 = router.resolve(role='goal', category='coding', bound_backend='codex', bound_args=['-m', 'gpt-5.3-codex'])
    assert r3.backend.name == 'codex'

    # Role policy blocks non-readonly-fence backend for root/judge
    soft = _SoftOnlyBackend(name='soft', command='echo')
    reg = BackendRegistry({'soft': soft, **router.registry.all()})
    cfg2 = load_config(_write_config(tmp_path))
    from dataclasses import replace
    from ancor.config import CategoryConfig
    cfg2 = replace(cfg2, categories={**cfg2.categories, 'root': CategoryConfig('root', 'soft', [])})
    router2 = BackendRouter(cfg2, reg)
    with pytest.raises(PermissionError, match='readonly fence'):
        router2.resolve(role='root')


def test_goal_runner_with_backend_router_binds_backend_and_session(tmp_path):
    repo_root = str(Path(__file__).resolve().parents[1])
    # Create a tiny executable fake codex script that emits thread.started JSONL and reports completion.
    fake_codex = tmp_path / 'fake_codex.py'
    fake_codex.write_text(
        f'''#!{sys.executable}
import json, os, sys
sys.path.insert(0, {repo_root!r})
from ancor.config import load_config
from ancor.core_api import AncorCore

print(json.dumps({{"type": "thread.started", "thread_id": "codex-thread-001"}}), flush=True)
core = AncorCore(load_config(store_path_override=os.environ["ANCOR_STORE_PATH"]))
gid = os.environ["GOAL_ID"]
core.goal_report(gid, {{
    "request_generation": 1,
    "status": "completed",
    "summary": "done via fake codex",
    "artifacts": [],
}})
''',
        encoding='utf-8',
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IEXEC)

    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(
        f'''
store_path: {tmp_path / 'requests.db'}
default_backend: codex
backends:
  codex:
    adapter: codex
    command: {fake_codex}
categories:
  coding:
    backend: codex
    args: ["-m", "test-model"]
''',
        encoding='utf-8',
    )
    cfg = load_config(cfg_path)
    core = AncorCore(cfg)
    created = core.request_create(title='Test multi-backend', fields={'outcome': ['run goal']})
    req_id = created['request']['id']
    goal = core.goal_spawn({
        'request_ref': {'id': req_id, 'generation': 1},
        'objective': 'Run via codex backend',
        'done_criteria': 'completed',
        'category': 'coding',
    })
    router = BackendRouter(cfg)
    runner = GoalRunner(core.goal_store, backend_router=router)
    rc = asyncio.run(runner.run_goal(goal['goal_id']))
    assert rc == 0
    refreshed = core.goal_store.get_goal(goal['goal_id'])
    assert refreshed['status'] == 'completed'
    assert refreshed['backend'] == 'codex'
    assert refreshed['backend_args'] == ['-m', 'test-model']
    assert refreshed['session_id'] == 'codex-thread-001'


def test_agy_backend_command_mcp_and_parsing(tmp_path):
    be = AgyBackend(name='agy', command='agy')
    mcp_cmd = [sys.executable, '-m', 'ancor.cli.main', 'mcp', '--role', 'judge']

    # Judge gets --mode plan --sandbox + workspace .agents/mcp_config.json
    cmd, env, tmp_files = be.build_command(LaunchSpec(
        role='judge',
        prompt='verify delivery',
        system_prompt='You are judge',
        extra_args=['--model', 'claude-opus-4-6', '--effort', 'high'],
        mcp_command=mcp_cmd,
        cwd=tmp_path,
    ))
    assert len(tmp_files) == 1 and tmp_files[0].exists()
    assert cmd[:3] == ['agy', '--output-format', 'stream-json']
    assert '--mode' in cmd and cmd[cmd.index('--mode') + 1] == 'plan'
    assert '--sandbox' in cmd
    assert '--dangerously-skip-permissions' not in cmd
    assert cmd[-2] == '--print'
    mcp_obj = json.loads(tmp_files[0].read_text(encoding='utf-8'))
    assert mcp_obj['mcpServers']['ancor']['command'] == sys.executable
    assert env['ANCOR_ROLE'] == 'judge'
    tmp_files[0].unlink(missing_ok=True)

    # Goal gets --dangerously-skip-permissions + --resume when resuming
    rcmd, _, rtmp = be.build_command(LaunchSpec(
        role='goal',
        goal_id='g-agy',
        session_id='conv-999',
        prompt='resume work',
        mcp_command=mcp_cmd,
        cwd=tmp_path,
    ))
    for p in rtmp:
        p.unlink(missing_ok=True)
    assert '--dangerously-skip-permissions' in rcmd
    assert '--resume' in rcmd and rcmd[rcmd.index('--resume') + 1] == 'conv-999'
    assert rcmd[-2:] == ['--print', 'resume work']

    ev = be.parse_output_line(json.dumps({'event': 'init', 'conversation_id': 'conv-abc'}))
    assert ev is not None and ev.kind == 'session' and ev.session_id == 'conv-abc'
    ev_err = be.parse_output_line(json.dumps({'event': 'result', 'result': {'error': 'quota reached'}}))
    assert ev_err is not None and ev_err.kind == 'error' and 'quota' in ev_err.error
    ev2 = be.parse_output_line('Conversation: conv-stderr', is_stderr=True)
    assert ev2 is not None and ev2.session_id == 'conv-stderr'


def test_dsh_backend_command_patch_and_readonly_capability():
    mcp_cmd = [sys.executable, '-m', 'ancor.cli.main', 'mcp', '--role', 'goal', '--goal-id', 'g1']

    # Default dsh has supports_readonly_fence=False
    be = DshBackend(name='dsh', command='dsh')
    assert be.capabilities.supports_readonly_fence is False
    cmd, env, temp_files = be.build_command(LaunchSpec(
        role='goal',
        goal_id='g1',
        session_id='dsh-sess-1',
        prompt='do work',
        extra_args=['--max-turns', '20'],
        mcp_command=mcp_cmd,
    ))
    assert cmd[:3] == ['dsh', '--profile', 'headless']
    assert '--patch' in cmd
    assert len(temp_files) == 1 and temp_files[0].exists()
    patch_data = json.loads(temp_files[0].read_text(encoding='utf-8'))
    assert patch_data['mcp_servers']['ancor']['command'] == sys.executable
    assert '--resume' in cmd and cmd[cmd.index('--resume') + 1] == 'dsh-sess-1'
    assert env['DSH_TELEMETRY_DISABLED'] == '1'
    temp_files[0].unlink(missing_ok=True)

    # With readonly_profile configured, supports_readonly_fence becomes True
    be_ro = DshBackend(name='dsh', command='dsh', extra={'readonly_profile': 'headless-ro'})
    assert be_ro.capabilities.supports_readonly_fence is True
    ro_cmd, _, ro_tmp = be_ro.build_command(LaunchSpec(role='judge', prompt='audit'))
    assert ro_cmd[:3] == ['dsh', '--profile', 'headless-ro']
    assert ro_tmp == []

    ev = be.parse_output_line(json.dumps({'session_id': 'dsh-42'}))
    assert ev is not None and ev.kind == 'session' and ev.session_id == 'dsh-42'
