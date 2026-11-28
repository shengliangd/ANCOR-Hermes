"""DeepSeek Harness (``dsh``) execution backend adapter."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from .base import BackendCapabilities, ExecutionBackend, LaunchSpec, OutputEvent


class DshBackend(ExecutionBackend):
    """Launches headless DeepSeek Harness sessions (``dsh --profile headless ...``).

    Follows ``dsh`` flag partitioning: framework flags (``--profile``, ``--patch``) precede
    runner/extra flags (``--resume``, prompt). Dynamic MCP injection uses a temporary
    overlay ``--patch`` YAML file cleaned up by GoalRunner after process exit.
    """

    def __init__(
        self,
        *,
        name: str,
        command: str,
        default_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        extra: dict | None = None,
    ):
        super().__init__(name=name, command=command, default_args=default_args, env=env, extra=extra)
        has_readonly_profile = bool(self.extra.get('readonly_profile'))
        self.capabilities = BackendCapabilities(
            supports_mcp=True,
            supports_resume=True,
            supports_readonly_fence=has_readonly_profile,
            supports_model_telemetry=True,
        )

    def _write_mcp_patch(self, spec: LaunchSpec) -> Path | None:
        if not spec.mcp_command:
            return None
        patch_obj = {
            'mcp_servers': {
                'ancor': {
                    'command': spec.mcp_command[0],
                    'args': spec.mcp_command[1:],
                }
            }
        }
        fd, raw_path = tempfile.mkstemp(prefix='ancor-dsh-mcp-', suffix='.json')
        path = Path(raw_path)
        with open(fd, 'w', encoding='utf-8') as fh:
            json.dump(patch_obj, fh, ensure_ascii=False, indent=2)
        return path

    def build_command(self, spec: LaunchSpec) -> tuple[list[str], dict[str, str], list[Path]]:
        env = self._base_env(spec)
        env.setdefault('DSH_TELEMETRY_DISABLED', '1')
        temp_files: list[Path] = []

        if spec.role in {'root', 'judge'} and self.extra.get('readonly_profile'):
            profile = str(self.extra['readonly_profile'])
        else:
            profile = str(self.extra.get('profile') or 'headless')

        cmd = [self.command, '--profile', profile]
        patch_path = self._write_mcp_patch(spec)
        if patch_path is not None:
            temp_files.append(patch_path)
            cmd.extend(['--patch', str(patch_path)])

        cmd.extend(self.default_args)
        cmd.extend(spec.extra_args)

        if spec.session_id:
            cmd.extend(['--resume', spec.session_id])

        prompt = spec.prompt
        if spec.system_prompt and not spec.session_id:
            prompt = f'{spec.system_prompt.strip()}\n\n---\n\n{spec.prompt}'
        cmd.append(prompt)
        return cmd, env, temp_files

    def parse_output_line(self, line: str, *, is_stderr: bool = False) -> OutputEvent | None:
        text = line.strip()
        if not text:
            return None
        if is_stderr:
            if text.startswith('Session: '):
                sid = text.removeprefix('Session: ').strip()
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

        for key in ('session_id', 'thread_id'):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                return OutputEvent(kind='session', session_id=val.strip())

        model = event.get('model') or event.get('response_model')
        if isinstance(model, str) and model.strip():
            provider = event.get('provider') or 'deepseek'
            return OutputEvent(kind='model', model=model.strip(), provider=str(provider))

        if str(event.get('type') or '').lower() == 'error' or event.get('error'):
            msg = event.get('message') or event.get('error')
            if msg:
                return OutputEvent(kind='error', error=str(msg))
        return None
