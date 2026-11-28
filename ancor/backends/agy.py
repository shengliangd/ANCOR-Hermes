"""Antigravity CLI (``agy``) execution backend adapter."""
from __future__ import annotations

import json
from pathlib import Path

from .base import BackendCapabilities, ExecutionBackend, LaunchSpec, OutputEvent


class AgyBackend(ExecutionBackend):
    """Launches headless Antigravity CLI sessions (``agy --print --output-format stream-json``)."""

    capabilities = BackendCapabilities(
        supports_mcp=True,
        supports_resume=True,
        supports_readonly_fence=True,
        supports_model_telemetry=True,
    )

    def _write_mcp_config(self, spec: LaunchSpec) -> tuple[Path | None, list[Path]]:
        """Write workspace ``.agents/mcp_config.json`` so ``agy`` auto-discovers ``ancor`` MCP."""
        if not spec.mcp_command:
            return None, []
        mcp_env = {
            k: v for k, v in spec.env.items()
            if k in {'ANCOR_STORE_PATH', 'ANCOR_CONFIG', 'ANCOR_MEMORY_DIR', 'PYTHONPATH'} and v
        }
        server_entry: dict = {
            'command': spec.mcp_command[0],
            'args': spec.mcp_command[1:],
        }
        if mcp_env:
            server_entry['env'] = mcp_env
        payload = {'mcpServers': {'ancor': server_entry}}

        base = spec.cwd if spec.cwd is not None else Path.cwd()
        agents_dir = base / '.agents'
        cfg_file = agents_dir / 'mcp_config.json'
        if cfg_file.exists():
            # Avoid clobbering a user's existing workspace mcp_config.json.
            return None, []
        agents_dir.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return cfg_file, [cfg_file]

    def build_command(self, spec: LaunchSpec) -> tuple[list[str], dict[str, str], list[Path]]:
        env = self._base_env(spec)
        prompt = spec.prompt
        if spec.system_prompt and not spec.session_id:
            prompt = f'{spec.system_prompt.strip()}\n\n---\n\n{spec.prompt}'

        cmd = [
            self.command,
            '--output-format', 'stream-json',
        ]
        if spec.role in {'root', 'judge'}:
            # Note: --disable-slash-commands disables --mode plan in agy, so omit it here.
            cmd.extend(['--mode', 'plan', '--sandbox'])
        else:
            cmd.append('--disable-slash-commands')
            if self.extra.get('skip_permissions', True):
                cmd.append('--dangerously-skip-permissions')
            mode = self.extra.get('mode')
            if mode:
                cmd.extend(['--mode', str(mode)])

        if spec.cwd is not None:
            cmd.extend(['--add-dir', str(spec.cwd)])

        _mcp_file, temp_files = self._write_mcp_config(spec)

        if spec.session_id:
            cmd.extend(['--resume', spec.session_id])

        cmd.extend(self.default_args)
        cmd.extend(spec.extra_args)
        # In agy, --print takes the prompt string as its argument value.
        cmd.extend(['--print', prompt])
        return cmd, env, temp_files

    def parse_output_line(self, line: str, *, is_stderr: bool = False) -> OutputEvent | None:
        text = line.strip()
        if not text:
            return None
        if is_stderr:
            if text.startswith('AGY_ERROR:'):
                return OutputEvent(kind='error', error=text.removeprefix('AGY_ERROR:').strip())
            for prefix in ('Conversation: ', 'Session: '):
                if text.startswith(prefix):
                    sid = text.removeprefix(prefix).strip()
                    if sid:
                        return OutputEvent(kind='session', session_id=sid)
            return None
        if not text.startswith('{'):
            return None
        try:
            event = json.loads(text)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None

        ev_name = str(event.get('event') or event.get('type') or '').lower()
        if ev_name == 'result' and isinstance(event.get('result'), dict):
            res = event['result']
            if res.get('error'):
                return OutputEvent(kind='error', error=str(res['error']))
            cid = res.get('conversation_id')
            if isinstance(cid, str) and cid.strip():
                return OutputEvent(kind='session', session_id=cid.strip())

        for key in ('conversation_id', 'conversationId', 'session_id', 'sessionId'):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                return OutputEvent(kind='session', session_id=val.strip())

        model = event.get('model') or event.get('response_model')
        if isinstance(model, str) and model.strip():
            provider = event.get('provider') or 'antigravity'
            return OutputEvent(kind='model', model=model.strip(), provider=str(provider))

        if ev_name == 'error' or event.get('error'):
            msg = event.get('message') or event.get('error')
            if msg:
                return OutputEvent(kind='error', error=str(msg))
        return None
