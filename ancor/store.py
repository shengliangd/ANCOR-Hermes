"""Durable requests, separate from the native task board.

Confirmation references are integration-supplied audit references, not proof
of human consent. Returned dictionaries are detached copies, never live state.
"""
from contextlib import contextmanager
import json
import sqlite3
import time
from uuid import uuid4

from .views import TITLE_MAX_CHARS
from .changes import Changes


STABLE_STATUSES = frozenset({'active', 'paused'})
AWAITING_ACCEPTANCE = 'awaiting_acceptance'
TERMINAL_STATUSES = frozenset({'accepted', 'cancelled', 'invalidated'})
TRANSITION_TARGETS = frozenset({'active', 'paused', 'cancelled', 'invalidated'})
NOTE_MAX_CHARS = 16000


class Store(Changes):
    def __init__(self, path):
        self.path = str(path)
        with self._transaction() as db:
            from .legacy import has_legacy_schema
            if has_legacy_schema(db):
                raise RuntimeError('Legacy ANCOR store: migrate an offline copy with python -m ancor.migrate_requests before opening this version')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, brief TEXT NOT NULL,
                    replacement_of TEXT, summary TEXT);
                CREATE TABLE IF NOT EXISTS cancellations (
                    id TEXT PRIMARY KEY, request_ids TEXT NOT NULL,
                    reason TEXT, summary TEXT, confirmation_reference TEXT);
                CREATE TABLE IF NOT EXISTS request_commands (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL,
                    request_id TEXT NOT NULL, reason TEXT, summary TEXT,
                    confirmation_reference TEXT);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    brief TEXT NOT NULL,
                    replacement_of TEXT, confirmation_reference TEXT NOT NULL,
                    status TEXT NOT NULL, target_status TEXT);
                CREATE TABLE IF NOT EXISTS task_links (
                    request_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    PRIMARY KEY (request_id, task_id));
                CREATE TABLE IF NOT EXISTS request_notes (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    body TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    deleted_at INTEGER);
                CREATE INDEX IF NOT EXISTS request_notes_scope
                    ON request_notes(request_id, deleted_at, updated_at);
                CREATE TABLE IF NOT EXISTS signal_proposals (
                    id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, source_key TEXT UNIQUE,
                    acknowledged INTEGER NOT NULL DEFAULT 0);
            ''')
            duplicates = db.execute(
                'SELECT task_id FROM task_links GROUP BY task_id HAVING COUNT(*) > 1').fetchall()
            if duplicates:
                raise ValueError('task ownership conflict: repair duplicate associations before opening store: '
                                 + ', '.join(row['task_id'] for row in duplicates))
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS task_links_owner ON task_links(task_id)')
            self._require_title_columns(db)
            self._ensure_columns(db, 'drafts', {'summary': 'TEXT'})
            self._ensure_columns(db, 'requests', {'target_status': 'TEXT'})
            self._drop_retired_columns(db)
            self._migrate_legacy_acceptances(db)
            self._init_changes(db)

    @staticmethod
    def _migrate_legacy_acceptances(db):
        """Move coordinator-only acceptance into the current human gate.

        Older releases committed ``accepted`` without recording a final human
        decision. Preserve that event, add durable verification evidence, and
        reopen the request for human acceptance. The source key makes the
        migration idempotent and the new event stays acknowledged so it does
        not create coordinator attention.
        """
        for row in db.execute(
                "SELECT * FROM requests WHERE status='accepted' ORDER BY rowid"):
            request = dict(row)
            accepted = None
            for event_row in db.execute(
                    "SELECT * FROM events WHERE kind='request.accepted' "
                    'ORDER BY id DESC'):
                event = Store._event(event_row)
                if event['payload'].get('request_id') == request['id']:
                    accepted = event
                    break
            if (accepted is None
                    or accepted['payload'].get('verification_event_id') is not None):
                continue
            source_key = ('migration.legacy-coordinator-acceptance:'
                          + str(accepted['id']))
            existing = db.execute(
                'SELECT id FROM events WHERE source_key=?', (source_key,)).fetchone()
            if existing is None:
                evidence = accepted['payload'].get('evidence')
                if evidence is None:
                    evidence = ('Legacy coordinator acceptance event '
                                f"{accepted['id']} preserved; no evidence text was stored.")
                Store._add_event(
                    db, 'request.verified', {
                        'request_id': request['id'],
                        'evidence': evidence,
                        'status': AWAITING_ACCEPTANCE,
                        'migrated_from_event_id': accepted['id'],
                        'migration_reason': 'legacy coordinator acceptance required '
                                            'the human final gate',
                    }, source_key=source_key, acknowledged=True)
            db.execute(
                "UPDATE requests SET status=?, target_status=NULL "
                "WHERE id=? AND status='accepted'",
                (AWAITING_ACCEPTANCE, request['id']))

    @staticmethod
    def _require_title_columns(db):
        for table in ('drafts', 'requests'):
            present = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            if 'title' not in present:
                raise RuntimeError(
                    f'ANCOR {table} schema has no title column; run '
                    '`python -m ancor.migrate_titles` before loading this version')

    @staticmethod
    def _ensure_columns(db, table, columns):
        present = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for column, kind in columns.items():
            if column not in present:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')

    @staticmethod
    def _drop_retired_columns(db):
        """Drop fields ANCOR no longer stores; SQLite 3.35+ handles this in place."""
        for table in ('drafts', 'requests'):
            present = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            if 'acceptance_criteria' in present:
                db.execute(f'ALTER TABLE {table} DROP COLUMN acceptance_criteria')

    @contextmanager
    def _transaction(self):
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

    @staticmethod
    def _text(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('nonempty text required')
        return value

    @staticmethod
    def _optional_text(value):
        """Convert omitted/blank optional inputs to the stored NULL value."""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError('optional text must be nonempty when provided')
        return value

    @staticmethod
    def _title(value):
        value = Store._text(value).strip()
        if '\n' in value or '\r' in value:
            raise ValueError('title must be a single line')
        if len(value) > TITLE_MAX_CHARS:
            raise ValueError(f'title must be at most {TITLE_MAX_CHARS} characters')
        return value

    @staticmethod
    def _get(db, table, identifier):
        row = db.execute(f'SELECT * FROM {table} WHERE id = ?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return dict(row)

    @staticmethod
    def _stable(request):
        return (request['status'] in STABLE_STATUSES
                and request.get('target_status') is None)

    def _require_replacement_bases(self, db, replacement_ids):
        bases = []
        for identifier in replacement_ids:
            try:
                base = self._get(db, 'requests', identifier)
            except KeyError:
                if db.execute('SELECT 1 FROM drafts WHERE id = ?', (identifier,)).fetchone():
                    raise ValueError(
                        f'replacement_of {identifier} is a draft, not a confirmed request') from None
                raise ValueError(
                    f'replacement_of {identifier} does not reference an existing request') from None
            if not self._stable(base):
                raise ValueError(
                    f'replacement_of {identifier} is {base["status"]}; '
                    'replacement bases must be stable active or paused requests') from None
            bases.append(base)
        return bases

    def create_draft(self, brief, replacement_of=None, summary=None, *, title):
        title = self._title(title)
        self._text(brief)
        summary = self._optional_text(summary)
        if isinstance(replacement_of, list):
            if not replacement_of or any(not isinstance(i, str) or not i.strip() for i in replacement_of):
                raise ValueError('replacement_of list must contain nonempty IDs')
            replacement_ids = list(dict.fromkeys(replacement_of))
            replacement_of = json.dumps(replacement_ids)
        else:
            replacement_of = self._optional_text(replacement_of)
            replacement_ids = [replacement_of] if replacement_of is not None else []
        identifier = uuid4().hex
        with self._transaction() as db:
            self._require_replacement_bases(db, replacement_ids)
            db.execute('INSERT INTO drafts (id, title, brief, replacement_of, summary) '
                       'VALUES (?, ?, ?, ?, ?)',
                       (identifier, title, brief, replacement_of, summary))
            return self._get(db, 'drafts', identifier)

    def get_draft(self, draft_id):
        with self._transaction() as db:
            return self._get(db, 'drafts', draft_id)

    @staticmethod
    def _cancellation(row):
        cancellation = dict(row)
        cancellation['request_ids'] = json.loads(cancellation['request_ids'])
        return cancellation

    def create_cancellation(self, request_ids, reason=None, summary=None):
        """Create an immutable cancellation proposal without changing requests."""
        ids = list(dict.fromkeys(request_ids))
        if not ids or any(not isinstance(i, str) or not i.strip() for i in ids):
            raise ValueError('request_ids must contain nonempty IDs')
        reason = self._optional_text(reason)
        summary = self._optional_text(summary)
        identifier = uuid4().hex
        with self._transaction() as db:
            rows = [self._get(db, 'requests', i) for i in ids]
            if any(not self._stable(row) for row in rows):
                raise ValueError('all requests must be stable active or paused')
            db.execute('INSERT INTO cancellations VALUES (?, ?, ?, ?, NULL)',
                       (identifier, json.dumps(ids), reason, summary))
            return self._cancellation(self._get(db, 'cancellations', identifier))

    def get_cancellation(self, cancellation_id):
        with self._transaction() as db:
            return self._cancellation(self._get(db, 'cancellations', cancellation_id))

    def create_request_command(self, action, request_id, reason, summary=None):
        """Create an immutable pause/resume proposal without changing status."""
        if action not in {'pause', 'resume'}:
            raise ValueError('unknown request command')
        reason = self._text(reason)
        summary = self._optional_text(summary)
        required = 'active' if action == 'pause' else 'paused'
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            if not self._stable(request) or request['status'] != required:
                raise ValueError(f'{action} requires a stable {required} request')
            existing = db.execute(
                'SELECT * FROM request_commands WHERE action=? AND request_id=? '
                'AND reason=? AND summary=? AND confirmation_reference IS NULL '
                'ORDER BY rowid LIMIT 1',
                (action, request_id, reason, summary)).fetchone()
            if existing:
                return dict(existing)
            identifier = uuid4().hex
            db.execute('INSERT INTO request_commands VALUES (?, ?, ?, ?, ?, NULL)',
                       (identifier, action, request_id, reason, summary))
            return dict(db.execute('SELECT * FROM request_commands WHERE id=?',
                                   (identifier,)).fetchone())

    def get_request_command(self, command_id):
        with self._transaction() as db:
            return self._get(db, 'request_commands', command_id)

    def confirm_request_command(self, command_id, confirmation_reference):
        """Request one exact pause/resume transition atomically and idempotently."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            command = self._get(db, 'request_commands', command_id)
            request = self._get(db, 'requests', command['request_id'])
            if command['confirmation_reference'] is not None:
                return request
            action = command['action']
            target = 'paused' if action == 'pause' else 'active'
            required = 'active' if action == 'pause' else 'paused'
            if not self._stable(request) or request['status'] != required:
                raise ValueError(f'{action} requires a stable {required} request')
            db.execute('UPDATE requests SET target_status=?,control_version=control_version+1 WHERE id=?',
                       (target, request['id']))
            self._add_event(db, 'request.transition_requested', {
                'request_id': request['id'], 'command_id': command_id,
                'from_status': request['status'], 'target_status': target,
                'reason': command['reason'], 'summary': command['summary'],
                'confirmation_reference': confirmation_reference})
            request = self._get(db, 'requests', request['id'])
            db.execute('UPDATE request_commands SET confirmation_reference=? WHERE id=?',
                       (confirmation_reference, command_id))
            return request

    def create_human_decision(self, request_id, action, feedback=None):
        """Prepare an immutable human accept/request-changes proposal."""
        if action not in {'accept', 'request_changes'}:
            raise ValueError('unknown human acceptance decision')
        feedback = self._optional_text(feedback)
        if action == 'request_changes' and feedback is None:
            raise ValueError('request_changes requires feedback')
        if action == 'accept' and feedback is not None:
            raise ValueError('accept does not accept feedback; use request_changes')
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            if (request['status'] != AWAITING_ACCEPTANCE
                    or request['target_status'] is not None):
                raise ValueError('request is not awaiting human acceptance')
            if self._latest_verification(db, request_id) is None:
                raise ValueError('coordinator verification evidence is missing')
            stored_action = ('human_accept' if action == 'accept'
                             else 'human_request_changes')
            existing = db.execute(
                'SELECT * FROM request_commands WHERE action=? AND request_id=? '
                'AND reason IS ? AND confirmation_reference IS NULL '
                'ORDER BY rowid LIMIT 1',
                (stored_action, request_id, feedback)).fetchone()
            verification = self._latest_verification(db, request_id)
            if existing:
                binding = db.execute('SELECT * FROM command_bindings WHERE command_id=?', (existing['id'],)).fetchone()
                if binding and binding['generation'] == request['generation'] and binding['verification_id'] == verification['id']:
                    return dict(existing)
            identifier = uuid4().hex
            db.execute('INSERT INTO command_bindings VALUES(?,?,?)', (identifier, request['generation'], verification['id']))
            db.execute('INSERT INTO request_commands VALUES (?, ?, ?, ?, ?, NULL)',
                       (identifier, stored_action, request_id, feedback, None))
            return dict(db.execute('SELECT * FROM request_commands WHERE id=?',
                                   (identifier,)).fetchone())

    def latest_verification(self, request_id):
        with self._transaction() as db:
            return self._latest_verification(db, request_id)

    def confirm_human_decision(self, command_id, confirmation_reference):
        """Commit one exact human acceptance decision idempotently."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            command = self._get(db, 'request_commands', command_id)
            if command['action'] not in {'human_accept', 'human_request_changes'}:
                raise ValueError('not a human acceptance decision')
            request = self._get(db, 'requests', command['request_id'])
            if command['confirmation_reference'] is not None:
                return request
            if (request['status'] != AWAITING_ACCEPTANCE
                    or request['target_status'] is not None):
                raise ValueError('request is not awaiting human acceptance')
            verification = self._latest_verification(db, request['id'])
            if verification is None:
                raise ValueError('coordinator verification evidence is missing')
            binding = db.execute('SELECT * FROM command_bindings WHERE command_id=?', (command_id,)).fetchone()
            if not binding or binding['generation'] != request['generation'] or binding['verification_id'] != verification['id']:
                raise RuntimeError('acceptance proposal is stale; prepare a new decision')
            if command['action'] == 'human_accept':
                db.execute("UPDATE requests SET status='accepted', target_status=NULL,control_version=control_version+1 "
                           'WHERE id=?', (request['id'],))
                self._add_event(
                    db, 'request.accepted',
                    {'request_id': request['id'],
                     'evidence': verification['payload']['evidence'],
                     'verification_event_id': verification['id'],
                     'confirmation_reference': confirmation_reference,
                     'command_id': command_id},
                    acknowledged=True)
            else:
                db.execute("UPDATE requests SET status='active', target_status=NULL,control_version=control_version+1 "
                           'WHERE id=?', (request['id'],))
                self._add_event(
                    db, 'request.changes_requested',
                    {'request_id': request['id'],
                     'feedback': command['reason'],
                     'verification_event_id': verification['id'],
                     'confirmation_reference': confirmation_reference,
                     'command_id': command_id})
            db.execute('UPDATE request_commands SET confirmation_reference=? WHERE id=?',
                       (confirmation_reference, command_id))
            return self._get(db, 'requests', request['id'])

    def get_request(self, request_id):
        with self._transaction() as db:
            return self._get(db, 'requests', request_id)

    @staticmethod
    def _note_body(value):
        body = Store._text(value)
        if len(body) > NOTE_MAX_CHARS:
            raise ValueError(f'note body must be at most {NOTE_MAX_CHARS} characters')
        return body

    @staticmethod
    def _require_note_mutable(request):
        if not Store._stable(request):
            raise ValueError(
                'notes can be changed only while the request is stable active or paused')

    def _get_note(self, db, request_id, note_id):
        row = db.execute(
            'SELECT * FROM request_notes WHERE id=? AND request_id=?',
            (note_id, request_id)).fetchone()
        if row is None:
            raise KeyError(note_id)
        return dict(row)

    def add_note(self, request_id, body, *, actor='ancor-coordinator'):
        body = self._note_body(body)
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            self._require_note_mutable(request)
            now = int(time.time())
            identifier = uuid4().hex
            db.execute(
                'INSERT INTO request_notes '
                '(id, request_id, body, revision, created_at, updated_at, deleted_at) '
                'VALUES (?, ?, ?, 1, ?, ?, NULL)',
                (identifier, request_id, body, now, now))
            note = self._get_note(db, request_id, identifier)
            self._note_history(db, note, actor)
            return note

    def get_note(self, request_id, note_id):
        with self._transaction() as db:
            self._get(db, 'requests', request_id)
            return self._get_note(db, request_id, note_id)

    def list_notes(self, request_id, limit=50, include_deleted=False):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit must be an integer between 1 and 200')
        if type(include_deleted) is not bool:
            raise ValueError('include_deleted must be a boolean')
        with self._transaction() as db:
            self._get(db, 'requests', request_id)
            where = 'request_id=?' + ('' if include_deleted else ' AND deleted_at IS NULL')
            total = db.execute(
                f'SELECT COUNT(*) FROM request_notes WHERE {where}',
                (request_id,)).fetchone()[0]
            rows = db.execute(
                f'SELECT * FROM request_notes WHERE {where} '
                'ORDER BY updated_at DESC, rowid DESC LIMIT ?',
                (request_id, limit)).fetchall()
            return {'items': [dict(row) for row in rows], 'count': total}

    def update_note(self, request_id, note_id, body, *, expected_revision=None, actor='ancor-coordinator'):
        body = self._note_body(body)
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            self._require_note_mutable(request)
            note = self._get_note(db, request_id, note_id)
            if expected_revision is not None and note['revision'] != expected_revision:
                raise RuntimeError('note version changed')
            if note['deleted_at'] is not None:
                raise ValueError('deleted note cannot be updated')
            db.execute(
                'UPDATE request_notes SET body=?, revision=revision+1, updated_at=? '
                'WHERE id=? AND request_id=?',
                (body, int(time.time()), note_id, request_id))
            note = self._get_note(db, request_id, note_id)
            self._note_history(db, note, actor)
            return note

    def delete_note(self, request_id, note_id, *, expected_revision=None, actor='ancor-coordinator'):
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            self._require_note_mutable(request)
            note = self._get_note(db, request_id, note_id)
            if expected_revision is not None and note['revision'] != expected_revision:
                raise RuntimeError('note version changed')
            if note['deleted_at'] is not None:
                return note
            now = int(time.time())
            db.execute(
                'UPDATE request_notes SET deleted_at=?, revision=revision+1, updated_at=? '
                'WHERE id=? AND request_id=?',
                (now, now, note_id, request_id))
            note = self._get_note(db, request_id, note_id)
            self._note_history(db, note, actor)
            return note

    @staticmethod
    def _event(row):
        event = dict(row)
        event['payload'] = json.loads(event['payload'])
        event['acknowledged'] = bool(event['acknowledged'])
        return event

    @staticmethod
    def _add_event(db, kind, payload, source_key=None, *, acknowledged=False):
        Store._text(kind)
        if source_key is not None:
            Store._text(source_key)
            existing = db.execute('SELECT * FROM events WHERE source_key = ?',
                                  (source_key,)).fetchone()
            if existing:
                return Store._event(existing)
        cursor = db.execute(
            'INSERT INTO events (kind, payload, source_key, acknowledged) VALUES (?, ?, ?, ?)',
            (kind, json.dumps(payload), source_key, int(acknowledged)))
        return Store._event(db.execute('SELECT * FROM events WHERE id = ?',
                                       (cursor.lastrowid,)).fetchone())

    def propose_signal(self, text, source):
        self._text(text)
        self._text(source)
        identifier = uuid4().hex
        with self._transaction() as db:
            db.execute('INSERT INTO signal_proposals VALUES (?, ?, ?, ?)',
                       (identifier, text, source, time.time()))
        return self.get_signal_proposal(identifier)

    def get_signal_proposal(self, identifier):
        with self._transaction() as db:
            row = db.execute('SELECT * FROM signal_proposals WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return dict(row)

    def confirm_signal(self, identifier, reference):
        self._text(reference)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM signal_proposals WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return self._add_event(db, 'interactor.signal', {
                'text': row['text'], 'source': row['source'], 'created_at': time.time(),
                'confirmation_reference': reference}, source_key='signal:' + identifier)

    def add_event(self, kind, payload, source_key=None):
        with self._transaction() as db:
            return self._add_event(db, kind, payload, source_key)

    def pending_events(self):
        with self._transaction() as db:
            return [self._event(row) for row in db.execute(
                'SELECT * FROM events WHERE acknowledged = 0 ORDER BY id')]

    def events_by_ids(self, event_ids):
        """Return retained events by ID, including already acknowledged rows."""
        identifiers = list(dict.fromkeys(event_ids))
        if not identifiers:
            return []
        placeholders = ','.join('?' for _ in identifiers)
        with self._transaction() as db:
            rows = db.execute(
                f'SELECT * FROM events WHERE id IN ({placeholders}) ORDER BY id',
                identifiers).fetchall()
        return [self._event(row) for row in rows]

    def ack_events(self, event_ids):
        with self._transaction() as db:
            db.executemany('UPDATE events SET acknowledged = 1 WHERE id = ?',
                           ((identifier,) for identifier in event_ids))

    def confirm(self, draft_id, confirmation_reference):
        """Stage a confirmed draft and any replaced requests for transition."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            existing = db.execute('SELECT * FROM requests WHERE draft_id = ?',
                                  (draft_id,)).fetchone()
            if existing:
                return dict(existing)
            draft = self._get(db, 'drafts', draft_id)
            replacement_ids = ([draft['replacement_of']] if draft['replacement_of'] is not None else [])
            if draft['replacement_of'] and draft['replacement_of'].startswith('['):
                replacement_ids = json.loads(draft['replacement_of'])
            bases = self._require_replacement_bases(db, replacement_ids)
            for base in bases:
                db.execute("UPDATE requests SET target_status = 'invalidated',control_version=control_version+1 WHERE id = ?",
                           (base['id'],))
            identifier = uuid4().hex
            status = 'pending' if bases else 'active'
            target_status = 'active' if bases else None
            db.execute('INSERT INTO requests '
                       '(id, draft_id, title, brief, replacement_of, '
                       'confirmation_reference, status, target_status) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (identifier, draft_id, draft['title'], draft['brief'],
                        draft['replacement_of'], confirmation_reference, status, target_status))
            for base in bases:
                self._add_event(db, 'request.transition_requested', {
                    'request_id': base['id'], 'replacement_id': identifier,
                    'from_status': base['status'], 'target_status': 'invalidated'})
            if bases:
                self._add_event(db, 'request.staged', {
                    'request_id': identifier, 'draft_id': draft_id,
                    'replacement_of': draft['replacement_of'],
                    'target_status': 'active'})
            else:
                self._add_event(db, 'request.activated', {
                    'request_id': identifier, 'draft_id': draft_id,
                    'from_status': None, 'target_status': 'active'})
            return self._get(db, 'requests', identifier)

    def link_task(self, request_id, task_id):
        self._text(task_id)
        with self._transaction() as db:
            self._get(db, 'requests', request_id)
            self._link_task(db, request_id, task_id)

    @staticmethod
    def _link_task(db, request_id, task_id):
        owner = db.execute('SELECT request_id FROM task_links WHERE task_id=?', (task_id,)).fetchone()
        if owner is not None and owner['request_id'] != request_id:
            raise ValueError(f"task ownership conflict: {task_id} belongs to {owner['request_id']}, not {request_id}")
        db.execute('INSERT OR IGNORE INTO task_links VALUES (?, ?)', (request_id, task_id))

    def snapshot(self):
        with self._transaction() as db:
            return {
                'drafts': [dict(row) for row in db.execute('SELECT * FROM drafts ORDER BY rowid')],
                'requests': [dict(row) for row in db.execute(
                    'SELECT * FROM requests ORDER BY rowid')],
                'task_links': [dict(row) for row in db.execute(
                    'SELECT * FROM task_links ORDER BY request_id, task_id')],
                'pending_events': [self._event(row) for row in db.execute(
                    'SELECT * FROM events WHERE acknowledged = 0 ORDER BY id')],
            }

    def acceptances(self):
        """Retain coordinator verification and final human acceptance evidence."""
        with self._transaction() as db:
            return [self._event(row) for row in db.execute(
                "SELECT * FROM events WHERE kind IN "
                "('request.verified', 'request.changes_requested', "
                "'request.accepted') ORDER BY id")]

    def request_transitions(self, request_id=None):
        """Retain request transition history after its events are handled."""
        with self._transaction() as db:
            events = [self._event(row) for row in db.execute(
                "SELECT * FROM events WHERE kind IN "
                "('request.staged', 'request.transition_requested', "
                "'request.activated', 'request.paused', 'request.resumed', "
                "'request.verified', 'request.changes_requested', "
                "'request.accepted', 'request.cancelled', "
                "'request.invalidated') ORDER BY id")]
        if request_id is not None:
            events = [event for event in events
                      if event['payload'].get('request_id') == request_id]
        return events

    @staticmethod
    def _replacement_ids(value):
        if value is None:
            return []
        if isinstance(value, str) and value.startswith('['):
            return json.loads(value)
        return [value]

    def _transition_event(self, db, request, target):
        request_id = request['id']
        payload = {
            'request_id': request_id,
            'from_status': request['status'],
            'target_status': target,
        }
        action = {'paused': 'pause', 'active': 'resume'}.get(target)
        requested = None
        if action is not None:
            command = db.execute(
                'SELECT * FROM request_commands WHERE request_id=? AND action=? '
                'AND confirmation_reference IS NOT NULL ORDER BY rowid DESC LIMIT 1',
                (request_id, action)).fetchone()
            if command:
                payload.update(command_id=command['id'], reason=command['reason'],
                               summary=command['summary'],
                               confirmation_reference=command['confirmation_reference'])
            else:
                requested = self._latest_transition_request(db, request_id, target)
                if requested:
                    payload.update({
                        key: requested.get(key) for key in
                        ('command_id', 'reason', 'summary', 'confirmation_reference')
                        if requested.get(key) is not None})
        if target == 'cancelled':
            found = False
            for row in db.execute(
                    'SELECT * FROM cancellations WHERE confirmation_reference IS NOT NULL '
                    'ORDER BY rowid DESC'):
                identifiers = json.loads(row['request_ids'])
                if request_id in identifiers:
                    payload.update(reason=row['reason'], summary=row['summary'],
                                   batch_request_ids=identifiers,
                                   confirmation_reference=row['confirmation_reference'])
                    found = True
                    break
            if not found:
                requested = self._latest_transition_request(db, request_id, target)
                if requested:
                    payload.update({
                        key: requested.get(key) for key in
                        ('reason', 'batch_request_ids', 'confirmation_reference')
                        if requested.get(key) is not None})
        if target == 'invalidated':
            for successor in db.execute(
                    "SELECT * FROM requests WHERE status='pending' "
                    "AND target_status='active' ORDER BY rowid"):
                if request_id in self._replacement_ids(successor['replacement_of']):
                    payload['replacement_id'] = successor['id']
                    break
        if target == 'active' and request['status'] == 'pending':
            replacement_ids = self._replacement_ids(request['replacement_of'])
            payload['replacement_of'] = request['replacement_of']
            if replacement_ids:
                payload['invalidated_request_ids'] = replacement_ids
        kind = ('request.paused' if target == 'paused' else
                'request.resumed' if target == 'active' and request['status'] == 'paused' else
                'request.activated' if target == 'active' else
                'request.cancelled' if target == 'cancelled' else
                'request.invalidated')
        requested = self._latest_transition_request(db, request_id, target)
        if requested and requested.get('change_id'):
            for key in ('command_id', 'reason', 'summary', 'confirmation_reference'):
                payload.pop(key, None)
            payload.update({key: requested[key] for key in ('change_id', 'reason', 'confirmation_reference') if key in requested})
        return kind, payload

    @staticmethod
    def _latest_transition_request(db, request_id, target):
        for row in db.execute(
                "SELECT payload FROM events WHERE kind='request.transition_requested' "
                'ORDER BY id DESC'):
            payload = json.loads(row['payload'])
            if (payload.get('request_id') == request_id
                    and payload.get('target_status') == target):
                return payload
        return None

    def _finalize_one(self, db, request):
        target = request.get('target_status')
        if target is None:
            return request
        if target not in TRANSITION_TARGETS:
            raise RuntimeError(f'unknown target request status {target!r}')
        if target == 'active' and request['status'] == 'pending':
            replacement_ids = self._replacement_ids(request['replacement_of'])
            if any(self._get(db, 'requests', identifier)['status'] != 'invalidated'
                   for identifier in replacement_ids):
                raise RuntimeError('replacement bases are not yet invalidated')
        kind, payload = self._transition_event(db, request, target)
        cursor = db.execute(
            'UPDATE requests SET status=?, target_status=NULL, control_version=control_version+1 '
            'WHERE id=? AND status=? AND target_status=?',
            (target, request['id'], request['status'], target))
        if cursor.rowcount != 1:
            raise RuntimeError('request transition changed concurrently')
        self._add_event(db, kind, payload)
        return self._get(db, 'requests', request['id'])

    def submit_acceptance(self, request_id, evidence, *, generation=None):
        """Record coordinator verification and wait for the human's final decision."""
        self._text(evidence)
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            if request['status'] != 'active' or request['target_status'] is not None:
                raise ValueError('request is not stably active')
            if generation is not None and request['generation'] != generation:
                raise RuntimeError('request changed during verification')
            if request['content_mode'] == 'points':
                self._check_verification(db, request, evidence)
            event = self._add_event(
                db, 'request.verified',
                {'request_id': request_id, 'evidence': evidence,
                 'generation': request['generation'], 'status': AWAITING_ACCEPTANCE})
            db.execute('UPDATE requests SET status=?,verification_id=? WHERE id=?',
                       (AWAITING_ACCEPTANCE, event['id'], request_id))
            # Verification itself needs no coordinator follow-up. Keeping this
            # audit event acknowledged prevents a redundant wake while the
            # request waits for the human gate.
            db.execute('UPDATE events SET acknowledged=1 WHERE id=?', (event['id'],))
            return self._get(db, 'requests', request_id)

    @staticmethod
    def _latest_verification(db, request_id):
        for row in db.execute(
                "SELECT * FROM events WHERE kind='request.verified' "
                'ORDER BY id DESC'):
            event = Store._event(row)
            if event['payload'].get('request_id') == request_id:
                return event
        return None

    def accept(self, request_id, confirmation_reference=None, feedback=None):
        """Commit the human's final acceptance of coordinator-verified work."""
        if confirmation_reference is not None:
            self._text(confirmation_reference)
        feedback = self._optional_text(feedback)
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            if (request['status'] != AWAITING_ACCEPTANCE
                    or request['target_status'] is not None):
                raise ValueError('request is not awaiting human acceptance')
            verification = self._latest_verification(db, request_id)
            if verification is None:
                raise ValueError('coordinator verification evidence is missing')
            if request['content_mode'] == 'points':
                self._check_verification(db, request, verification['payload']['evidence'])
            db.execute("UPDATE requests SET status='accepted', target_status=NULL,control_version=control_version+1 "
                       'WHERE id=?', (request_id,))
            self._add_event(
                db, 'request.accepted',
                {'request_id': request_id,
                 'evidence': verification['payload']['evidence'],
                 'verification_event_id': verification['id'],
                 'confirmation_reference': confirmation_reference,
                 **({'feedback': feedback} if feedback else {})},
                acknowledged=True)
            return self._get(db, 'requests', request_id)

    def request_changes(self, request_id, feedback, confirmation_reference=None):
        """Return verified work to active coordination with exact human feedback."""
        feedback = self._text(feedback)
        if confirmation_reference is not None:
            self._text(confirmation_reference)
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            if (request['status'] != AWAITING_ACCEPTANCE
                    or request['target_status'] is not None):
                raise ValueError('request is not awaiting human acceptance')
            verification = self._latest_verification(db, request_id)
            if verification is None:
                raise ValueError('coordinator verification evidence is missing')
            db.execute("UPDATE requests SET status='active', target_status=NULL,control_version=control_version+1 "
                       'WHERE id=?', (request_id,))
            self._add_event(
                db, 'request.changes_requested',
                {'request_id': request_id, 'feedback': feedback,
                 'verification_event_id': verification['id'],
                 'confirmation_reference': confirmation_reference})
            return self._get(db, 'requests', request_id)

    def _request_cancel(self, db, request_ids, reason):
        ids = list(dict.fromkeys(request_ids))
        if not ids or any(not isinstance(i, str) or not i.strip() for i in ids):
            raise ValueError('request_ids must contain nonempty IDs')
        rows = [self._get(db, 'requests', i) for i in ids]
        if any(not self._stable(row) for row in rows):
            raise ValueError('all requests must be stable active or paused')
        for row in rows:
            db.execute("UPDATE requests SET target_status = 'cancelled',control_version=control_version+1 WHERE id = ?",
                       (row['id'],))
            self._add_event(db, 'request.transition_requested', {
                'request_id': row['id'], 'from_status': row['status'],
                'target_status': 'cancelled', 'reason': reason,
                'batch_request_ids': ids})
        return [self._get(db, 'requests', i) for i in ids]

    def cancel(self, request_ids, reason):
        """Request cancellation of stable requests; advance commits it."""
        self._text(reason)
        with self._transaction() as db:
            return self._request_cancel(db, request_ids, reason)

    def confirm_cancellation(self, cancellation_id, confirmation_reference):
        """Consume one exact cancellation confirmation idempotently and atomically."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            cancellation = self._get(db, 'cancellations', cancellation_id)
            ids = json.loads(cancellation['request_ids'])
            if cancellation['confirmation_reference'] is not None:
                return [self._get(db, 'requests', i) for i in ids]
            rows = self._request_cancel(
                db, ids, cancellation['reason'] or 'User confirmed cancellation')
            db.execute('UPDATE cancellations SET confirmation_reference = ? WHERE id = ?',
                       (confirmation_reference, cancellation_id))
            return rows

    def finalize_transition(self, request_id):
        """Commit a verified transition and activate any replacement made ready by it."""
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            if request['target_status'] is None:
                return {'request': request, 'activated': []}
            target = request['target_status']
            finalized = self._finalize_one(db, request)
            activated = []
            if target == 'invalidated':
                for successor in db.execute(
                        "SELECT * FROM requests WHERE status='pending' "
                        "AND target_status='active' ORDER BY rowid").fetchall():
                    successor = dict(successor)
                    replacement_ids = self._replacement_ids(successor['replacement_of'])
                    if request_id not in replacement_ids:
                        continue
                    if all(self._get(db, 'requests', identifier)['status'] == 'invalidated'
                           for identifier in replacement_ids):
                        activated.append(self._finalize_one(db, successor))
            return {'request': finalized, 'activated': activated}

    def reject(self, request_id, reason):
        self._text(reason)
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            if request['status'] != 'active' or request['target_status'] is not None:
                raise ValueError('request is not stably active')
            self._add_event(db, 'request.rejected',
                            {'request_id': request_id, 'reason': reason})
            return request


    def associate_task(self, task_id, request_id, reason):
        self._text(task_id); self._text(reason)
        with self._transaction() as db:
            request = self._get(db, 'requests', request_id)
            if request['status'] != 'active' or request['target_status'] is not None:
                raise ValueError('association target must be stably active')
            self._link_task(db, request_id, task_id)
            self._add_event(db, 'task.associated', {'task_id': task_id, 'request_id': request_id, 'reason': reason})
            return {'task_id': task_id, 'request_id': request_id, 'reason': reason}
