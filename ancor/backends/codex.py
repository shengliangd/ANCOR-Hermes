"""Codex CLI execution backend adapter (``codex exec --json`` + dynamic MCP injection)."""
from __future__ import annotations

import json
from pathlib import Path

from .base import BackendCapabilities, ExecutionBackend, LaunchSpec, OutputEvent


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_str_list(values: list[str]) -> str:
    return '[' + ', '.join(_toml_str(v) for v in values) + ']'


class CodexBackend(ExecutionBackend):
    """Launches headless Codex CLI sessions with per-role ANCOR MCP tool injection."""

    capabilities = BackendCapabilities(
        supports_mcp=True,
        supports_resume=True,
        supports_readonly_fence=True,
        supports_model_telemetry=True,
    )

    def _mcp_config_args(self, spec: LaunchSpec) -> list[str]:
        if not spec.mcp_command:
            return []
        exe = spec.mcp_command[0]
        mcp_args = spec.mcp_command[1:]
        out = [
            '-c', f'mcp_servers.ancor.command={_toml_str(exe)}',
            '-c', f'mcp_servers.ancor.args={_toml_str_list(mcp_args)}',
        ]
        # Pass through critical env vars to the MCP server subprocess (Codex strips inherited env).
        mcp_env = {
            k: v for k, v in spec.env.items()
            if k in {'ANCOR_STORE_PATH', 'ANCOR_CONFIG', 'ANCOR_MEMORY_DIR', 'PYTHONPATH'} and v
        }
        if mcp_env:
            pairs = ', '.join(f'{k} = {_toml_str(v)}' for k, v in sorted(mcp_env.items()))
            out.extend(['-c', f'mcp_servers.ancor.env={{{pairs}}}'])
        return out

    def build_command(self, spec: LaunchSpec) -> tuple[list[str], dict[str, str], list[Path]]:
        env = self._base_env(spec)
        mcp_args = self._mcp_config_args(spec)
        prompt = spec.prompt
        if spec.system_prompt and not spec.session_id:
            prompt = f'{spec.system_prompt.strip()}\n\n---\n\n{spec.prompt}'

        if spec.session_id:
            cmd = [
                self.command,
                'exec', 'resume',
                '--json',
                '--skip-git-repo-check',
                *mcp_args,
                *self.default_args,
                *spec.extra_args,
            ]
            if spec.role == 'goal' and '--dangerously-bypass-approvals-and-sandbox' not in cmd:
                if self.extra.get('bypass_sandbox', True):
                    cmd.append('--dangerously-bypass-approvals-and-sandbox')
            cmd.extend([spec.session_id, prompt])
            return cmd, env, []

        cmd = [
            self.command,
            'exec',
            '--json',
            '--skip-git-repo-check',
        ]
        if spec.role in {'root', 'judge'}:
            cmd.extend(['-s', 'read-only'])
        elif self.extra.get('sandbox'):
            cmd.extend(['-s', str(self.extra['sandbox'])])
        elif self.extra.get('bypass_sandbox', True):
            cmd.append('--dangerously-bypass-approvals-and-sandbox')
        else:
            cmd.extend(['-s', 'workspace-write'])

        if spec.cwd is not None:
            cmd.extend(['-C', str(spec.cwd)])

        cmd.extend(mcp_args)
        cmd.extend(self.default_args)
        cmd.extend(spec.extra_args)
        cmd.append(prompt)
        return cmd, env, []

    def parse_output_line(self, line: str, *, is_stderr: bool = False) -> OutputEvent | None:
        if is_stderr:
            return None
        text = line.strip()
        if not text or not text.startswith('{'):
            return None
        try:
            event = json.loads(text)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        ev_type = event.get('type')
        if ev_type == 'thread.started':
            tid = event.get('thread_id')
            if isinstance(tid, str) and tid.strip():
                return OutputEvent(kind='session', session_id=tid.strip())
        if ev_type in {'turn.completed', 'response.completed'}:
            model = event.get('model')
            if isinstance(model, str) and model.strip():
                return OutputEvent(kind='model', model=model.strip(), provider='openai')
        if ev_type == 'error':
            msg = event.get('message') or event.get('error')
            if msg:
                return OutputEvent(kind='error', error=str(msg))
        return None
