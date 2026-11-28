"""NotificationSink protocol and payload types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class SinkError(RuntimeError):
    """Permanent/uncertain delivery error from a notification sink."""


class SinkUnavailable(SinkError):
    """Transient unavailability; safe to defer and retry."""


@dataclass(frozen=True)
class NotificationPayload:
    id: str
    kind: str
    message: str
    request_id: str | None = None
    task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'kind': self.kind,
            'message': self.message,
            'request_id': self.request_id,
            'task_id': self.task_id,
        }


class NotificationSink:
    name: str
    kinds: frozenset[str]
    timeout_seconds: float

    def __init__(self, *, name: str, kinds: frozenset[str], timeout_seconds: float = 60.0):
        self.name = name
        self.kinds = kinds
        self.timeout_seconds = timeout_seconds

    def accepts(self, kind: str) -> bool:
        return kind in self.kinds

    def send(self, payload: NotificationPayload) -> dict[str, Any]:
        raise NotImplementedError
