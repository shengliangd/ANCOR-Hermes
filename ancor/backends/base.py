"""ExecutionBackend protocol, capability declarations, and launch contracts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import signal
from typing import Literal

RoleName = Literal['root', 'goal', 'judge']


@dataclass(frozen=True)
class BackendCapabilities:
    supports_mcp: bool = True
    supports_resume: bool = True
    supports_readonly_fence: bool = False
    supports_model_telemetry: bool = False


@dataclass
class LaunchSpec:
    role: RoleName
    prompt: str
    system_prompt: str = ''
    goal_id: str | None = None
    session_id: str | None = None
    session_title: str | None = None
    extra_args: list[str] = field(default_factory=list)
    mcp_command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None


@dataclass(frozen=True)
class OutputEvent:
    kind: Literal['session', 'model', 'error']
    session_id: str | None = None
    model: str | None = None
    provider: str | None = None
    error: str | None = None


class ExecutionBackend:
    """Base adapter for launching, observing, and stopping headless CLI agent sessions."""

    name: str = 'base'
    capabilities: BackendCapabilities = BackendCapabilities()

    def __init__(
        self,
        *,
        name: str,
        command: str,
        default_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        extra: dict | None = None,
    ):
        self.name = name
        self.command = command
        self.default_args = list(default_args or [])
        self.env = dict(env or {})
        self.extra = dict(extra or {})

    def build_command(self, spec: LaunchSpec) -> tuple[list[str], dict[str, str], list[Path]]:
        raise NotImplementedError

    def parse_output_line(self, line: str, *, is_stderr: bool = False) -> OutputEvent | None:
        return None

    async def stop_session(self, pid: int | None, session_id: str | None = None) -> bool:
        """Default graceful stop: SIGTERM then SIGKILL on the process group."""
        if not pid or pid <= 0:
            return True
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return True
        except OSError:
            pgid = None

        for sig, wait_s in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            try:
                if pgid == pid:
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            deadline = asyncio.get_running_loop().time() + wait_s
            while asyncio.get_running_loop().time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                await asyncio.sleep(0.05)
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True

    def check_available(self) -> tuple[bool, str]:
        resolved = shutil.which(self.command)
        if not resolved:
            return False, f'executable {self.command!r} not found in PATH'
        return True, resolved

    def _base_env(self, spec: LaunchSpec) -> dict[str, str]:
        merged = dict(os.environ)
        for key in list(merged):
            if key.startswith('HERMES_SESSION_') or key in {
                'HERMES_UI_SESSION_ID',
                'HERMES_BROWSER_CONTROL_PRINCIPAL',
                'HERMES_BROWSER_CONTROL_TRANSPORT_FAMILY',
                'HERMES_CRON_SESSION',
                'GOAL_ID',
                'ANCOR_GOAL_ID',
                'ANCOR_ROLE',
            }:
                merged.pop(key, None)
        merged.update(self.env)
        merged.update(spec.env)
        merged['ANCOR_ROLE'] = spec.role
        if spec.goal_id:
            merged['GOAL_ID'] = spec.goal_id
            merged['ANCOR_GOAL_ID'] = spec.goal_id
        return merged
