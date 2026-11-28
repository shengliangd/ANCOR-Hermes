"""Generic CLI command + args notification sink (covers Hermes send, notify-send, custom scripts)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from .base import NotificationPayload, NotificationSink, SinkError, SinkUnavailable


class CommandNotifier(NotificationSink):
    def __init__(
        self,
        *,
        name: str,
        command: str,
        args: list[str] | None = None,
        input_mode: str = 'stdin_json',
        env: dict[str, str] | None = None,
        kinds: frozenset[str] = frozenset({'question', 'completion'}),
        timeout_seconds: float = 60.0,
    ):
        super().__init__(name=name, kinds=kinds, timeout_seconds=timeout_seconds)
        self.command = command
        self.args = list(args or [])
        self.input_mode = input_mode
        self.env = dict(env or {})

    def _render_args(self, payload: NotificationPayload) -> list[str]:
        if self.input_mode != 'template_args':
            return list(self.args)
        mapping = {
            'id': payload.id,
            'kind': payload.kind,
            'message': payload.message,
            'request_id': payload.request_id or '',
            'task_id': payload.task_id or '',
        }
        return [arg.format_map(mapping) for arg in self.args]

    def send(self, payload: NotificationPayload) -> dict[str, Any]:
        if not shutil.which(self.command) and not os.path.isabs(self.command):
            raise SinkUnavailable(f'notifier command {self.command!r} not found in PATH')
        argv = [self.command, *self._render_args(payload)]
        stdin_data: str | None = None
        if self.input_mode == 'stdin_json':
            stdin_data = json.dumps(payload.to_dict(), ensure_ascii=False)
        elif self.input_mode == 'stdin_text':
            stdin_data = payload.message

        env = dict(os.environ)
        env.update(self.env)
        env['ANCOR_NOTIFICATION_ID'] = payload.id
        env['ANCOR_NOTIFICATION_KIND'] = payload.kind
        if payload.request_id:
            env['ANCOR_REQUEST_ID'] = payload.request_id

        try:
            proc = subprocess.run(
                argv,
                input=stdin_data,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=env,
            )
        except FileNotFoundError as exc:
            raise SinkUnavailable(f'notifier executable not found: {exc}') from None
        except subprocess.TimeoutExpired:
            raise SinkError(f'notifier {self.name!r} timed out after {self.timeout_seconds}s') from None
        except OSError as exc:
            raise SinkError(f'notifier {self.name!r} os error: {exc}') from None

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or '').strip()[:300]
            raise SinkError(f'notifier {self.name!r} exited {proc.returncode}: {detail}')
        return {'ok': True, 'notifier': self.name, 'returncode': proc.returncode}
