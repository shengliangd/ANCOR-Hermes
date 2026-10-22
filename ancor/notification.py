"""Durable human notifications with Gateway-owned asynchronous delivery.

No worker event is automatically forwarded. The coordinator selects a neutral
question or verified completion and only enqueues it; the Gateway owner claims
and delivers it through Hermes' generic ``send_message_tool``. ``sent`` means
the native transport accepted the message, not that a human read or answered it.
External delivery is at-most-once after an uncertain outcome: exactly-once
cannot be promised across a process crash or transport timeout.
"""
from contextlib import contextmanager
import asyncio
import hashlib
import json
import logging
import sqlite3
import time

log = logging.getLogger(__name__)


def completion_requirement_id(row):
    """Return a completion's requirement, including legacy rows missing the column."""
    requirement_id = (row or {}).get('requirement_id')
    if requirement_id:
        return requirement_id
    identifier = (row or {}).get('id')
    if isinstance(identifier, str) and identifier.startswith('accept:'):
        return identifier[len('accept:'):] or None
    return None


class SenderError(RuntimeError):
    """Allowlisted transport diagnostics, never native output or message text."""

    def __init__(self, reason_code, *, exception_class='RuntimeError'):
        self.diagnostics = {'exception_class': exception_class, 'reason_code': reason_code}
        super().__init__(reason_code)


class TransportUnavailable(SenderError):
    """No outbound attempt was made, so the durable item may remain pending."""


class NativeSender:
    """Use the live Gateway adapter for the configured transport profile.

    Hermes owns target parsing and platform-specific send behavior. This class
    deliberately contains no platform names or SDK imports.
    """

    def __init__(self, profile):
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError('explicit transport profile required')
        self.profile = profile.strip()

    def _adapter(self, target):
        try:
            from gateway.run import _gateway_runner_ref
        except Exception as exc:
            raise TransportUnavailable('gateway_bridge_unavailable',
                                       exception_class=type(exc).__name__) from None
        runner = _gateway_runner_ref()
        if runner is None:
            raise TransportUnavailable('gateway_unavailable')
        platform_name = target.partition(':')[0].strip().lower()
        try:
            adapters = runner._adapters_for_profile(self.profile)
        except Exception as exc:
            raise TransportUnavailable('transport_profile_unavailable',
                                       exception_class=type(exc).__name__) from None
        for platform, adapter in (adapters or {}).items():
            if getattr(platform, 'value', str(platform)).lower() != platform_name:
                continue
            if adapter is None or not getattr(adapter, 'is_connected', True):
                break
            return adapter
        raise TransportUnavailable('transport_unavailable')

    def available(self, target):
        self._adapter(target)

    def __call__(self, target, message):
        self.available(target)
        try:
            from gateway.run import _profile_runtime_scope
            from hermes_cli.profiles import get_profile_dir
            from tools.send_message_tool import send_message_tool
        except Exception as exc:
            raise TransportUnavailable('native_send_bridge_unavailable',
                                       exception_class=type(exc).__name__) from None
        try:
            with _profile_runtime_scope(get_profile_dir(self.profile)):
                raw = send_message_tool({'action': 'send', 'target': target,
                                         'message': message})
        except TransportUnavailable:
            raise
        except Exception as exc:
            raise SenderError('send_exception', exception_class=type(exc).__name__) from None
        try:
            receipt = json.loads(raw)
        except (TypeError, ValueError):
            if isinstance(raw, dict):
                receipt = raw
            else:
                raise SenderError('invalid_receipt_json', exception_class='ValueError') from None
        if not isinstance(receipt, dict):
            raise SenderError('invalid_receipt_shape')
        if receipt.get('error') or receipt.get('skipped') or receipt.get('success') is not True:
            raise SenderError('delivery_unconfirmed')
        return receipt


class NotificationOutbox:
    """SQLite notification lifecycle. Closed questions are not answered questions."""

    def __init__(self, path, *, coordinator_profile, target=None, clock=time.time):
        if not coordinator_profile:
            raise ValueError('coordinator profile required')
        if target is not None and (not isinstance(target, str) or ':' not in target
                                   or not target.partition(':')[2].strip()):
            raise ValueError('explicit platform:destination required')
        self.path = str(path)
        self.coordinator_profile = coordinator_profile
        self.target = target
        self.clock = clock
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS human_notifications (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, message TEXT NOT NULL,
                target TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT,
                error TEXT, answer TEXT, answer_reference TEXT,
                requirement_id TEXT, task_id TEXT, queued_at REAL,
                delivery_started_at REAL, next_attempt_at REAL,
                closed_at REAL, closed_by TEXT, close_reason TEXT)''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(human_notifications)')}
            additions = {
                'requirement_id': 'TEXT', 'task_id': 'TEXT', 'queued_at': 'REAL',
                'delivery_started_at': 'REAL', 'next_attempt_at': 'REAL',
                'closed_at': 'REAL', 'closed_by': 'TEXT', 'close_reason': 'TEXT',
            }
            for column, kind in additions.items():
                if column not in columns:
                    db.execute(f'ALTER TABLE human_notifications ADD COLUMN {column} {kind}')
            # A response proposal binds the exact question and the exact reply
            # before the interactor asks the human for native confirmation.
            db.execute('''CREATE TABLE IF NOT EXISTS response_proposals (
                id TEXT PRIMARY KEY, notification_id TEXT NOT NULL,
                answer TEXT NOT NULL, message TEXT NOT NULL,
                requirement_id TEXT, task_id TEXT,
                superseded INTEGER NOT NULL DEFAULT 0,
                confirmed INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL)''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _coordinator(self, actor):
        if actor != self.coordinator_profile:
            raise PermissionError('only the coordinator selects human notifications')

    @staticmethod
    def _row(db, identifier):
        row = db.execute('SELECT * FROM human_notifications WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        result = dict(row)
        for column in ('requirement_id', 'task_id', 'queued_at', 'delivery_started_at',
                       'next_attempt_at', 'closed_at', 'closed_by', 'close_reason'):
            result.setdefault(column, None)
        result['receipt'] = json.loads(result['receipt']) if result['receipt'] else None
        result['closed'] = result['closed_at'] is not None
        return result

    @staticmethod
    def _proposal(db, proposal_id):
        row = db.execute('SELECT * FROM response_proposals WHERE id=?',
                         (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        result = dict(row)
        result['superseded'] = bool(result['superseded'])
        result['confirmed'] = bool(result['confirmed'])
        return result

    def get(self, identifier):
        with self._db() as db:
            return self._row(db, identifier)

    def list(self):
        with self._db() as db:
            return [self._row(db, row[0])
                    for row in db.execute('SELECT id FROM human_notifications ORDER BY rowid')]

    @classmethod
    def read_existing(cls, path):
        """Host inspection without configuring transport or creating an outbox."""
        from pathlib import Path
        path = Path(path)
        if not path.exists():
            return []
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            return [cls._row(db, row[0]) for row in db.execute(
                'SELECT id FROM human_notifications ORDER BY rowid')]
        finally:
            db.close()

    def enqueue(self, identifier, kind, message, *, actor, requirement_id=None, task_id=None):
        self._coordinator(actor)
        if not isinstance(self.target, str) or ':' not in self.target:
            raise ValueError('human_target is required to enqueue notifications')
        if kind not in ('question', 'completion'):
            raise ValueError('only questions and completion notices are supported')
        if not all(isinstance(v, str) and v.strip() for v in (identifier, message)):
            raise ValueError('nonempty identifier and message required')
        if 'MEDIA:' in message:
            raise ValueError('notifications are text-only; MEDIA markers forbidden')
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO human_notifications '
                       '(id, kind, message, target, state, requirement_id, task_id, queued_at) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (identifier, kind, message, self.target, 'pending',
                        requirement_id, task_id, self.clock()))
            row = self._row(db, identifier)
            if (row['kind'], row['message'], row['target'], row['requirement_id'], row['task_id']) != (
                    kind, message, self.target, requirement_id, task_id):
                raise ValueError('notification identifier already has different content')
            return row

    def claim_next(self, now=None):
        """Atomically claim one pending item for the single Gateway dispatcher."""
        now = self.clock() if now is None else now
        with self._db() as db:
            row = db.execute(
                "SELECT id FROM human_notifications WHERE state='pending' "
                'AND closed_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?) '
                'ORDER BY rowid LIMIT 1', (now,)).fetchone()
            if row is None:
                return None
            identifier = row[0]
            changed = db.execute(
                "UPDATE human_notifications SET state='sending',delivery_started_at=?,"
                'next_attempt_at=NULL,error=NULL WHERE id=? AND state=\'pending\'',
                (now, identifier))
            if changed.rowcount != 1:
                return None
            return self._row(db, identifier)

    def mark_sent(self, identifier, receipt):
        encoded = json.dumps(receipt)
        with self._db() as db:
            db.execute("UPDATE human_notifications SET state='sent',receipt=?,error=NULL "
                       "WHERE id=? AND state='sending'", (encoded, identifier))
            return self._row(db, identifier)

    def mark_uncertain(self, identifier, exc):
        diagnostics = (exc.diagnostics if isinstance(exc, SenderError)
                       else {'exception_class': type(exc).__name__,
                             'reason_code': 'send_exception'})
        with self._db() as db:
            db.execute("UPDATE human_notifications SET state='uncertain',error=?,"
                       "next_attempt_at=NULL WHERE id=? AND state='sending'",
                       (json.dumps(diagnostics), identifier))
            return self._row(db, identifier)

    def settle_unsent(self, identifier):
        """Stop a claimed, not-yet-sent question when it became terminal."""
        with self._db() as db:
            db.execute(
                "UPDATE human_notifications SET state=CASE "
                "WHEN answer IS NOT NULL THEN 'answered' "
                "WHEN closed_at IS NOT NULL THEN 'closed' ELSE state END,"
                "next_attempt_at=NULL WHERE id=? AND state='sending'", (identifier,))
            return self._row(db, identifier)

    def defer(self, identifier, *, reason_code, retry_at):
        diagnostics = {'exception_class': 'TransportUnavailable', 'reason_code': reason_code}
        with self._db() as db:
            db.execute("UPDATE human_notifications SET state='pending',error=?,"
                       "delivery_started_at=NULL,next_attempt_at=? "
                       "WHERE id=? AND state='sending' AND closed_at IS NULL",
                       (json.dumps(diagnostics), retry_at, identifier))
            return self._row(db, identifier)

    def close_question(self, identifier, *, closed_by, reason):
        if not all(isinstance(v, str) and v.strip() for v in (closed_by, reason)):
            raise ValueError('closed_by and close reason required')
        with self._db() as db:
            row = self._row(db, identifier)
            if row['kind'] != 'question':
                raise ValueError('only questions can be closed')
            if row['closed_at'] is not None:
                if row['close_reason'] == reason:
                    return row
                raise ValueError('question already closed for a different reason')
            if row['answer'] is not None:
                raise ValueError('answered questions cannot be closed')
            db.execute('UPDATE human_notifications SET closed_at=?,closed_by=?,close_reason=? '
                       'WHERE id=? AND closed_at IS NULL',
                       (self.clock(), closed_by, reason, identifier))
            return self._row(db, identifier)

    def close_delivery(self, identifier, *, closed_by, reason):
        """Close one undelivered notice for a structural reason, without an answer."""
        if not all(isinstance(value, str) and value.strip()
                   for value in (identifier, closed_by, reason)):
            raise ValueError('identifier, actor and close reason required')
        with self._db() as db:
            row = self._row(db, identifier)
            if row['closed_at'] is not None:
                return row
            db.execute(
                "UPDATE human_notifications SET closed_at=?, closed_by=?, close_reason=?, "
                "state=CASE WHEN state='sending' THEN state ELSE 'closed' END "
                'WHERE id=? AND closed_at IS NULL',
                (self.clock(), closed_by, reason, identifier))
            return self._row(db, identifier)

    def close_questions(self, decisions, *, closed_by, now=None):
        """Close unanswered open questions in one transaction.

        ``decisions`` maps notification ID to an allowlisted reason code.
        """
        if not decisions:
            return []
        now = self.clock() if now is None else now
        with self._db() as db:
            for identifier, reason in decisions.items():
                db.execute("UPDATE human_notifications SET closed_at=?,closed_by=?,close_reason=? "
                           "WHERE id=? AND kind='question' AND answer IS NULL AND closed_at IS NULL",
                           (now, closed_by, reason, identifier))
            return [self._row(db, identifier) for identifier in decisions]

    def respond(self, identifier, answer, reference):
        """Record a foreground human response; never resurrect a closed question."""
        if not all(isinstance(v, str) and v.strip() for v in (answer, reference)):
            raise ValueError('answer and source reference required')
        with self._db() as db:
            row = self._row(db, identifier)
            if row['kind'] != 'question':
                raise ValueError('not a question')
            if row['closed_at'] is not None:
                raise ValueError('question is closed: ' + (row['close_reason'] or 'closed'))
            if row['answer'] is not None and (row['answer'], row['answer_reference']) != (answer, reference):
                raise ValueError('question already answered')
            if row['state'] == 'pending':
                db.execute("UPDATE human_notifications SET answer=?,answer_reference=?,"
                           "state='answered',next_attempt_at=NULL WHERE id=?",
                           (answer, reference, identifier))
            else:
                db.execute('UPDATE human_notifications SET answer=?,answer_reference=? WHERE id=?',
                           (answer, reference, identifier))
            return self._row(db, identifier)

    def propose_response(self, identifier, answer):
        """Bind one exact reply to its still-open question without recording it.

        A newer proposal supersedes older unconfirmed proposals for the same
        question, so confirming a stale reply cannot silently win.
        """
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('nonempty answer required')
        with self._db() as db:
            row = self._row(db, identifier)
            if row['kind'] != 'question':
                raise ValueError('not a question')
            if row['closed_at'] is not None:
                raise ValueError('question is closed: ' + (row['close_reason'] or 'closed'))
            if row['answer'] is not None:
                raise ValueError('question already answered')
            proposal_id = hashlib.sha256(
                (identifier + '\0' + answer).encode('utf-8')).hexdigest()
            db.execute('UPDATE response_proposals SET superseded=1 '
                       'WHERE notification_id=? AND id<>? AND confirmed=0',
                       (identifier, proposal_id))
            db.execute('INSERT OR IGNORE INTO response_proposals('
                       'id, notification_id, answer, message, requirement_id, '
                       'task_id, superseded, confirmed, created_at) '
                       'VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)',
                       (proposal_id, identifier, answer, row['message'],
                        row.get('requirement_id'), row.get('task_id'), self.clock()))
            db.execute('UPDATE response_proposals SET superseded=0 '
                       'WHERE id=? AND confirmed=0', (proposal_id,))
            return self._proposal(db, proposal_id)

    def get_response_proposal(self, proposal_id):
        with self._db() as db:
            proposal = self._proposal(db, proposal_id)
            if proposal['superseded']:
                raise ValueError('response proposal superseded by a newer reply')
            return proposal

    def confirm_response(self, proposal_id, reference):
        """Commit exactly one previously proposed reply as a confirmed answer."""
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError('confirmation reference required')
        with self._db() as db:
            proposal = self._proposal(db, proposal_id)
            if proposal['superseded']:
                raise ValueError('response proposal superseded by a newer reply')
            row = self._row(db, proposal['notification_id'])
            if row['kind'] != 'question':
                raise ValueError('not a question')
            if row['closed_at'] is not None:
                raise ValueError('question is closed: ' + (row['close_reason'] or 'closed'))
            if row['answer'] is not None:
                if row['answer'] != proposal['answer']:
                    raise ValueError('question already answered')
                if proposal['confirmed']:
                    return row
            if row['state'] == 'pending':
                db.execute('UPDATE human_notifications SET answer=?,answer_reference=?,'
                           "state='answered',next_attempt_at=NULL WHERE id=?",
                           (proposal['answer'], reference, proposal['notification_id']))
            else:
                db.execute('UPDATE human_notifications SET answer=?,answer_reference=? WHERE id=?',
                           (proposal['answer'], reference, proposal['notification_id']))
            db.execute('UPDATE response_proposals SET confirmed=1 WHERE id=?', (proposal_id,))
            return self._row(db, proposal['notification_id'])

    def withdraw(self, identifier, *, actor, reason):
        self._coordinator(actor)
        return self.close_question(identifier, closed_by=actor, reason=reason)

    def close_completions(self, requirement_id, *, closed_by, reason, now=None):
        """Close every outstanding completion notice after a human decision."""
        if not all(isinstance(value, str) and value.strip()
                   for value in (requirement_id, closed_by, reason)):
            raise ValueError('requirement, actor and close reason required')
        now = self.clock() if now is None else now
        with self._db() as db:
            rows = [self._row(db, row[0]) for row in db.execute(
                "SELECT id FROM human_notifications WHERE kind='completion' "
                'AND (requirement_id=? OR id=?) AND closed_at IS NULL ORDER BY rowid',
                (requirement_id, 'accept:' + requirement_id))]
            for row in rows:
                db.execute(
                    'UPDATE human_notifications SET closed_at=?, closed_by=?, close_reason=?, '
                    "state=CASE WHEN state='sending' THEN state ELSE 'closed' END "
                    'WHERE id=? AND closed_at IS NULL',
                    (now, closed_by, reason, row['id']))
            return [self._row(db, row['id']) for row in rows]


class NotificationDispatcher:
    """One Gateway-owned delivery loop; transport-neutral and non-blocking."""

    def __init__(self, outbox, sender, *, resolve_requirement, resolve_task,
                 retry_seconds=30.0, timeout=180.0, clock=time.time):
        self.outbox = outbox
        self.sender = sender
        self.resolve_requirement = resolve_requirement
        self.resolve_task = resolve_task
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.timeout = timeout
        self.clock = clock

    def _gate(self, row):
        if row['kind'] == 'completion':
            requirement_id = completion_requirement_id(row)
            if requirement_id is None:
                return 'close', 'requirement_missing'
            try:
                requirement = self.resolve_requirement(requirement_id)
            except KeyError:
                return 'close', 'requirement_missing'
            target = requirement.get('target_status')
            if target is not None:
                return 'close', 'completion_no_longer_pending'
            if requirement.get('status') != 'awaiting_acceptance':
                return 'close', 'completion_no_longer_pending'
            return 'send', None
        if row['kind'] != 'question':
            return 'send', None
        try:
            requirement = self.resolve_requirement(row['requirement_id'])
        except KeyError:
            return 'close', 'requirement_missing'
        target = requirement.get('target_status')
        if target in {'cancelled', 'invalidated'}:
            return 'close', 'requirement_' + target
        if target is not None:
            return 'defer', 'requirement_transition_pending'
        status = requirement.get('status')
        if status == 'paused':
            return 'defer', 'requirement_paused'
        if status in {'cancelled', 'invalidated', 'accepted'}:
            return 'close', 'requirement_' + status
        if status != 'active':
            return 'close', 'requirement_not_active'
        task = self.resolve_task(row.get('task_id'))
        if task is not None and task.get('status') != 'blocked':
            return 'close', 'task_not_blocked'
        return 'send', None

    def _claim_and_gate(self):
        row = self.outbox.claim_next()
        if row is None:
            return None, None, None
        try:
            action, reason = self._gate(row)
        except Exception:
            return row, 'defer', 'eligibility_unavailable'
        return row, action, reason

    async def deliver_once(self):
        row, action, reason = await asyncio.to_thread(self._claim_and_gate)
        if row is None:
            return False
        if action == 'close':
            await asyncio.to_thread(self.outbox.close_delivery, row['id'],
                                    closed_by='host', reason=reason)
            return True
        if action == 'defer':
            retry_at = self.clock() + self.retry_seconds
            await asyncio.to_thread(self.outbox.defer, row['id'],
                                    reason_code=reason, retry_at=retry_at)
            return True
        current = await asyncio.to_thread(self.outbox.get, row['id'])
        if current['answer'] is not None or current['closed_at'] is not None:
            await asyncio.to_thread(self.outbox.settle_unsent, row['id'])
            return True
        try:
            receipt = await asyncio.wait_for(
                asyncio.to_thread(self.sender, row['target'], row['message']),
                timeout=self.timeout)
            if (not isinstance(receipt, dict) or receipt.get('success') is not True
                    or receipt.get('error') or receipt.get('skipped')):
                raise SenderError('delivery_unconfirmed')
        except asyncio.TimeoutError:
            await asyncio.to_thread(self.outbox.mark_uncertain, row['id'],
                                    SenderError('timeout', exception_class='TimeoutError'))
        except TransportUnavailable as exc:
            retry_at = self.clock() + self.retry_seconds
            await asyncio.to_thread(self.outbox.defer, row['id'],
                                    reason_code=exc.diagnostics['reason_code'],
                                    retry_at=retry_at)
        except Exception as exc:
            await asyncio.to_thread(self.outbox.mark_uncertain, row['id'], exc)
        else:
            await asyncio.to_thread(self.outbox.mark_sent, row['id'], receipt)
        return True

    async def run(self, is_connected):
        while is_connected():
            try:
                progressed = await self.deliver_once()
            except Exception:
                log.exception('ANCOR notification delivery iteration failed')
                progressed = False
            if not progressed:
                await asyncio.sleep(self.retry_seconds)
