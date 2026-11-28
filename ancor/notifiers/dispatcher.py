"""Multi-sink fan-out notification dispatcher over NotificationOutbox."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Callable

from ..config import NotifierConfig
from ..notification import NotificationOutbox
from ..views import completion_id
from .base import NotificationPayload, NotificationSink, SinkError, SinkUnavailable
from .command import CommandNotifier
from .webhook import WebhookNotifier

log = logging.getLogger(__name__)


def build_notifiers(configs: list[NotifierConfig]) -> list[NotificationSink]:
    sinks: list[NotificationSink] = []
    for cfg in configs:
        if cfg.type == 'command':
            assert cfg.command is not None
            sinks.append(CommandNotifier(
                name=cfg.name,
                command=cfg.command,
                args=cfg.args,
                input_mode=cfg.input_mode,
                env=cfg.env,
                kinds=cfg.kinds,
                timeout_seconds=cfg.timeout_seconds,
            ))
        elif cfg.type == 'webhook':
            assert cfg.url is not None
            sinks.append(WebhookNotifier(
                name=cfg.name,
                url=cfg.url,
                headers=cfg.headers,
                kinds=cfg.kinds,
                timeout_seconds=cfg.timeout_seconds,
            ))
    return sinks


class MultiNotificationDispatcher:
    """Delivers each outbox notification independently to all matching configured sinks."""

    def __init__(
        self,
        outbox: NotificationOutbox,
        sinks: list[NotificationSink],
        *,
        resolve_request: Callable[[str], dict],
        retry_seconds: float = 30.0,
        clock=time.time,
    ):
        self.outbox = outbox
        self.sinks = list(sinks)
        self.sinks_by_name = {s.name: s for s in self.sinks}
        self.resolve_request = resolve_request
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.clock = clock
        self._init_table()

    def _init_table(self) -> None:
        with self.outbox._db() as db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    notification_id TEXT NOT NULL,
                    notifier_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    receipt TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (notification_id, notifier_name)
                )
            ''')

    def _gate(self, row: dict) -> tuple[str, str | None]:
        if row['kind'] == 'completion':
            try:
                req = self.resolve_request(row['request_id'])
            except KeyError:
                return 'close', 'request_missing'
            if req.get('target_status') is not None or req.get('status') != 'awaiting_acceptance':
                return 'close', 'completion_no_longer_pending'
            if req.get('content_mode') == 'points' and row['id'] != completion_id(req):
                return 'close', 'verification_superseded'
            return 'send', None
        if row['kind'] != 'question':
            return 'send', None
        try:
            req = self.resolve_request(row['request_id'])
        except KeyError:
            return 'close', 'request_missing'
        target = req.get('target_status')
        if target in {'cancelled', 'invalidated'}:
            return 'close', 'request_' + target
        if target is not None:
            return 'defer', 'request_transition_pending'
        status = req.get('status')
        if status == 'paused':
            return 'defer', 'request_paused'
        if status in {'cancelled', 'invalidated', 'accepted'}:
            return 'close', 'request_' + status
        if status != 'active':
            return 'close', 'request_not_active'
        return 'send', None

    def _seed_deliveries(self) -> None:
        if not self.sinks:
            return
        now = float(self.clock())
        with self.outbox._db() as db:
            rows = db.execute(
                "SELECT id, kind FROM human_notifications "
                "WHERE closed_at IS NULL AND answer IS NULL AND state IN ('pending', 'sent')"
            ).fetchall()
            for row in rows:
                nid, kind = row['id'], row['kind']
                for sink in self.sinks:
                    if sink.accepts(kind):
                        db.execute(
                            'INSERT OR IGNORE INTO notification_deliveries'
                            '(notification_id, notifier_name, state, attempts, next_attempt_at, updated_at) '
                            "VALUES (?, ?, 'pending', 0, NULL, ?)",
                            (nid, sink.name, now),
                        )

    def _claim_next(self) -> tuple[dict, str] | tuple[None, None]:
        now = float(self.clock())
        with self.outbox._db() as db:
            row = db.execute(
                "SELECT d.notification_id, d.notifier_name FROM notification_deliveries d "
                "JOIN human_notifications n ON n.id = d.notification_id "
                "WHERE d.state='pending' AND n.closed_at IS NULL AND n.answer IS NULL "
                'AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?) '
                'ORDER BY n.rowid, d.notifier_name LIMIT 1',
                (now,),
            ).fetchone()
            if row is None:
                return None, None
            nid, sname = row['notification_id'], row['notifier_name']
            db.execute(
                "UPDATE notification_deliveries SET state='sending', attempts=attempts+1, updated_at=? "
                'WHERE notification_id=? AND notifier_name=?',
                (now, nid, sname),
            )
            notif = self.outbox._row(db, nid)
            return notif, sname

    def _update_delivery(
        self,
        notification_id: str,
        notifier_name: str,
        *,
        state: str,
        receipt: dict | None = None,
        error: str | None = None,
        next_attempt_at: float | None = None,
    ) -> None:
        now = float(self.clock())
        with self.outbox._db() as db:
            db.execute(
                'UPDATE notification_deliveries SET state=?, receipt=?, error=?, next_attempt_at=?, updated_at=? '
                'WHERE notification_id=? AND notifier_name=?',
                (
                    state,
                    json.dumps(receipt, ensure_ascii=False) if receipt is not None else None,
                    error,
                    next_attempt_at,
                    now,
                    notification_id,
                    notifier_name,
                ),
            )
            # Mark parent notification as 'sent' once at least one sink succeeded.
            if state == 'sent':
                db.execute(
                    "UPDATE human_notifications SET state='sent' WHERE id=? AND state='pending'",
                    (notification_id,),
                )

    async def deliver_once(self) -> bool:
        if not self.sinks:
            return False
        await asyncio.to_thread(self._seed_deliveries)
        notif, sink_name = await asyncio.to_thread(self._claim_next)
        if notif is None or sink_name is None:
            return False

        sink = self.sinks_by_name.get(sink_name)
        if sink is None:
            await asyncio.to_thread(
                self._update_delivery, notif['id'], sink_name, state='closed', error='sink_removed')
            return True

        action, reason = self._gate(notif)
        if action == 'close':
            await asyncio.to_thread(
                self.outbox.close_delivery, notif['id'], closed_by='host', reason=reason or 'closed')
            await asyncio.to_thread(
                self._update_delivery, notif['id'], sink_name, state='closed', error=reason)
            return True
        if action == 'defer':
            retry_at = float(self.clock()) + self.retry_seconds
            await asyncio.to_thread(
                self._update_delivery, notif['id'], sink_name, state='pending', error=reason, next_attempt_at=retry_at)
            return True

        payload = NotificationPayload(
            id=notif['id'],
            kind=notif['kind'],
            message=notif['message'],
            request_id=notif.get('request_id'),
            task_id=notif.get('task_id'),
        )
        try:
            receipt = await asyncio.wait_for(
                asyncio.to_thread(sink.send, payload),
                timeout=sink.timeout_seconds + 5.0,
            )
        except SinkUnavailable as exc:
            retry_at = float(self.clock()) + self.retry_seconds
            await asyncio.to_thread(
                self._update_delivery, notif['id'], sink_name, state='pending',
                error=str(exc), next_attempt_at=retry_at)
        except (SinkError, asyncio.TimeoutError, Exception) as exc:
            log.warning('ANCOR notifier %s failed for %s: %s', sink_name, notif['id'], exc)
            await asyncio.to_thread(
                self._update_delivery, notif['id'], sink_name, state='uncertain', error=str(exc))
        else:
            await asyncio.to_thread(
                self._update_delivery, notif['id'], sink_name, state='sent', receipt=receipt)
        return True

    def delivery_records(self, notification_id: str | None = None) -> list[dict]:
        with self.outbox._db() as db:
            if notification_id:
                rows = db.execute(
                    'SELECT * FROM notification_deliveries WHERE notification_id=? ORDER BY notifier_name',
                    (notification_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    'SELECT * FROM notification_deliveries ORDER BY notification_id, notifier_name'
                ).fetchall()
            return [dict(r) for r in rows]

    async def run(self, is_running: Callable[[], bool]) -> None:
        while is_running():
            try:
                progressed = await self.deliver_once()
            except Exception:
                log.exception('ANCOR multi-notifier iteration failed')
                progressed = False
            if not progressed:
                await asyncio.sleep(self.retry_seconds)
