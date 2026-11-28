"""HTTP Webhook notification sink using stdlib urllib (zero extra dependencies)."""
from __future__ import annotations

import json
from typing import Any
from urllib import error as urlerror, request as urlrequest

from .base import NotificationPayload, NotificationSink, SinkError, SinkUnavailable


class WebhookNotifier(NotificationSink):
    def __init__(
        self,
        *,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        kinds: frozenset[str] = frozenset({'question', 'completion'}),
        timeout_seconds: float = 60.0,
    ):
        super().__init__(name=name, kinds=kinds, timeout_seconds=timeout_seconds)
        self.url = url
        self.headers = dict(headers or {})

    def send(self, payload: NotificationPayload) -> dict[str, Any]:
        body = json.dumps(payload.to_dict(), ensure_ascii=False).encode('utf-8')
        headers = {'Content-Type': 'application/json; charset=utf-8', **self.headers}
        req = urlrequest.Request(self.url, data=body, headers=headers, method='POST')
        try:
            with urlrequest.urlopen(req, timeout=self.timeout_seconds) as resp:
                status = getattr(resp, 'status', 200)
                if 200 <= status < 300:
                    return {'ok': True, 'notifier': self.name, 'status': status}
                raise SinkError(f'webhook {self.name!r} returned HTTP {status}')
        except urlerror.HTTPError as exc:
            if exc.code in {429, 502, 503, 504}:
                raise SinkUnavailable(f'webhook {self.name!r} transient HTTP {exc.code}') from None
            raise SinkError(f'webhook {self.name!r} HTTP {exc.code}') from None
        except urlerror.URLError as exc:
            raise SinkUnavailable(f'webhook {self.name!r} unreachable: {exc.reason}') from None
        except TimeoutError:
            raise SinkError(f'webhook {self.name!r} timed out') from None
