"""Leader-selected durable notifications through the public ``hermes send`` CLI.

No worker event is automatically forwarded. Bind actor to trusted plugin context,
not model arguments. Configure an explicit destination and the transport-owning
Hermes profile; no credentials are read or copied here. ``sent`` means native
transport success, NOT human read/acceptance. A crash/timeout leaves ``uncertain``
(or ``sending`` after process death), requiring operator reconciliation before
retry: exactly-once external delivery cannot be promised by this CLI.
"""
from contextlib import contextmanager
import json
import sqlite3
import subprocess
import shutil


class SenderError(RuntimeError):
    """Allowlisted transport diagnostics, never subprocess output or message text."""
    def __init__(self, reason_code, resolved_executable, exit_code=None,
                 exception_class='RuntimeError'):
        self.diagnostics = dict(exception_class=exception_class, reason_code=reason_code,
                                exit_code=exit_code, resolved_executable=resolved_executable)
        super().__init__(reason_code)


class HermesSender:
    def __init__(self, profile, executable='hermes', timeout=60):
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError('explicit transport profile required')
        self.profile, self.executable, self.timeout = profile, executable, timeout

    def __call__(self, target, message):
        resolved = shutil.which(self.executable)
        try:
            result = subprocess.run(
                [self.executable, '--profile', self.profile, 'send', '--to', target,
                 '--file', '-', '--json'], input=message, text=True,
                capture_output=True, timeout=self.timeout, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            reason = ('timeout' if isinstance(exc, subprocess.TimeoutExpired) else
                      'executable_not_found' if isinstance(exc, FileNotFoundError) else
                      'executable_os_error')
            raise SenderError(reason, resolved, exception_class=type(exc).__name__) from None
        if result.returncode:
            raise SenderError('nonzero_exit', resolved, result.returncode)
        try:
            receipt = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise SenderError('invalid_receipt_json', resolved, result.returncode,
                              exception_class='ValueError') from None
        if not isinstance(receipt, dict):
            raise SenderError('invalid_receipt_shape', resolved, result.returncode)
        if receipt.get('error') or receipt.get('skipped') or receipt.get('success') is not True:
            raise SenderError('delivery_unconfirmed', resolved, result.returncode)
        return receipt


class NotificationOutbox:
    def __init__(self, path, *, leader_profile, target, sender):
        if not leader_profile or not isinstance(target, str) or ':' not in target or not target.partition(':')[2].strip():
            raise ValueError('leader and explicit platform:destination required')
        self.path = str(path)
        self.leader_profile, self.target, self.sender = leader_profile, target, sender
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS human_notifications (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, message TEXT NOT NULL,
                target TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT,
                error TEXT, answer TEXT, answer_reference TEXT)''')

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

    def _leader(self, actor):
        if actor != self.leader_profile:
            raise PermissionError('only the leader selects human notifications')

    @staticmethod
    def _row(db, identifier):
        row = db.execute('SELECT * FROM human_notifications WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        result = dict(row)
        result['receipt'] = json.loads(result['receipt']) if result['receipt'] else None
        return result

    def get(self, identifier):
        with self._db() as db:
            return self._row(db, identifier)

    def list(self):
        with self._db() as db:
            return [self._row(db, row[0]) for row in db.execute('SELECT id FROM human_notifications ORDER BY rowid')]

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

    def enqueue(self, identifier, kind, message, *, actor):
        self._leader(actor)
        if kind not in ('question', 'completion'):
            raise ValueError('only questions and completion notices are supported')
        if not all(isinstance(v, str) and v.strip() for v in (identifier, message)):
            raise ValueError('nonempty identifier and message required')
        # The native sender interprets MEDIA markers as file attachments.
        if 'MEDIA:' in message:
            raise ValueError('notifications are text-only; MEDIA markers forbidden')
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO human_notifications VALUES (?,?,?,?,?,NULL,NULL,NULL,NULL)',
                       (identifier, kind, message, self.target, 'pending'))
            row = self._row(db, identifier)
            if (row['kind'], row['message'], row['target']) != (kind, message, self.target):
                raise ValueError('notification identifier already has different content')
        return self.get(identifier)

    def deliver(self, identifier, *, actor):
        self._leader(actor)
        with self._db() as db:
            row = self._row(db, identifier)
            if row['state'] != 'pending':
                return row
            db.execute("UPDATE human_notifications SET state='sending' WHERE id=?", (identifier,))
        try:
            receipt = self.sender(row['target'], row['message'])
            if not isinstance(receipt, dict) or receipt.get('success') is not True or receipt.get('error') or receipt.get('skipped'):
                raise RuntimeError('sender did not confirm delivery')
            encoded = json.dumps(receipt)
        except Exception as exc:
            # Do not store raw subprocess output/exceptions: may contain credentials.
            with self._db() as db:
                db.execute("UPDATE human_notifications SET state='uncertain',error=? WHERE id=?",
                           (json.dumps(exc.diagnostics) if isinstance(exc, SenderError)
                            else type(exc).__name__, identifier))
        else:
            with self._db() as db:
                db.execute("UPDATE human_notifications SET state='sent',receipt=? WHERE id=?", (encoded, identifier))
        return self.get(identifier)

    def respond(self, identifier, answer, reference):
        """Call only from the foreground human-response integration, not a worker tool.

        Reference is an audit link, not authentication. Answers never implicitly
        accept requirements or authorize external actions.
        """
        if not all(isinstance(v, str) and v.strip() for v in (answer, reference)):
            raise ValueError('answer and source reference required')
        with self._db() as db:
            row = self._row(db, identifier)
            if row['kind'] != 'question':
                raise ValueError('not a question')
            if row['answer'] is not None and (row['answer'], row['answer_reference']) != (answer, reference):
                raise ValueError('question already answered')
            db.execute('UPDATE human_notifications SET answer=?,answer_reference=? WHERE id=?',
                       (answer, reference, identifier))
        return self.get(identifier)
