"""Hermes CLI execution backend adapter."""
from __future__ import annotations

import json
from pathlib import Path

from .base import BackendCapabilities, ExecutionBackend, LaunchSpec, OutputEvent


class HermesBackend(ExecutionBackend):
    """Launches headless Hermes sessions (``hermes -p <profile> --cli chat --yolo ...``)."""

    capabilities = BackendCapabilities(
        supports_mcp=True,
        supports_resume=True,
        supports_readonly_fence=True,
        supports_model_telemetry=True,
    )

    def _profile_name(self) -> str:
        args = self.default_args
        for idx, arg in enumerate(args):
            if arg in {'-p', '--profile'} and idx + 1 < len(args):
                return args[idx + 1]
        return str(self.extra.get('profile') or 'ancor-exec')

    def build_command(self, spec: LaunchSpec) -> tuple[list[str], dict[str, str], list[Path]]:
        env = self._base_env(spec)
        # Preserve launch identity for Hermes plugin role-fence compatibility.
        from ..session_identity import launch_identity
        env['HERMES_EPHEMERAL_SYSTEM_PROMPT'] = launch_identity(spec.role, spec.goal_id)

        cmd = [
            self.command,
            *self.default_args,
            '--cli', 'chat', '--yolo',
            '--format', 'stream-json',
            *spec.extra_args,
        ]
        if spec.session_id:
            cmd.extend(['--resume', spec.session_id])
        elif spec.session_title:
            cmd.extend(['-c', spec.session_title, '--create-if-missing'])
        cmd.extend(['-q', spec.prompt])
        return cmd, env, []

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
        try:
            event = json.loads(text)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        if event.get('type') in {'system', 'result'}:
            sid = event.get('session_id')
            if isinstance(sid, str) and sid.strip():
                return OutputEvent(kind='session', session_id=sid.strip())
        model = event.get('model') or event.get('response_model')
        if isinstance(model, str) and model.strip():
            provider = event.get('provider')
            return OutputEvent(
                kind='model',
                model=model.strip(),
                provider=provider.strip() if isinstance(provider, str) and provider.strip() else None,
            )
        return None

    async def stop_session(self, pid: int | None, session_id: str | None = None) -> bool:
        stopped_proc = await super().stop_session(pid, session_id)
        if session_id:
            try:
                from ..session_control import stop_native_session
                await stop_native_session(session_id, profile=self._profile_name())
            except Exception:
                pass
        return stopped_proc
