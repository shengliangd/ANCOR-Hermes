"""Durable requirements, separate from the native task board.

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
            db.executescript('''
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, brief TEXT NOT NULL,
                    replacement_of TEXT, summary TEXT);
                CREATE TABLE IF NOT EXISTS cancellations (
                    id TEXT PRIMARY KEY, requirement_ids TEXT NOT NULL,
                    reason TEXT, summary TEXT, confirmation_reference TEXT);
                CREATE TABLE IF NOT EXISTS requirement_commands (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL,
                    requirement_id TEXT NOT NULL, reason TEXT, summary TEXT,
                    confirmation_reference TEXT);
                CREATE TABLE IF NOT EXISTS requirements (
                    id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    brief TEXT NOT NULL,
                    replacement_of TEXT, confirmation_reference TEXT NOT NULL,
                    status TEXT NOT NULL, target_status TEXT);
                CREATE TABLE IF NOT EXISTS task_links (
                    requirement_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    PRIMARY KEY (requirement_id, task_id));
                CREATE TABLE IF NOT EXISTS requirement_notes (
                    id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL,
                    body TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    deleted_at INTEGER);
                CREATE INDEX IF NOT EXISTS requirement_notes_scope
                    ON requirement_notes(requirement_id, deleted_at, updated_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, source_key TEXT UNIQUE,
                    acknowledged INTEGER NOT NULL DEFAULT 0);
            ''')
            self._require_title_columns(db)
            self._ensure_columns(db, 'drafts', {'summary': 'TEXT'})
            self._ensure_columns(db, 'requirements', {'target_status': 'TEXT'})
            self._drop_retired_columns(db)
            self._migrate_legacy_acceptances(db)
            self._init_changes(db)

    @staticmethod
    def _migrate_legacy_acceptances(db):
        """Move coordinator-only acceptance into the current human gate.

        Older releases committed ``accepted`` without recording a final human
        decision. Preserve that event, add durable verification evidence, and
        reopen the requirement for human acceptance. The source key makes the
        migration idempotent and the new event stays acknowledged so it does
        not create coordinator attention.
        """
        for row in db.execute(
                "SELECT * FROM requirements WHERE status='accepted' ORDER BY rowid"):
            requirement = dict(row)
            accepted = None
            for event_row in db.execute(
                    "SELECT * FROM events WHERE kind='requirement.accepted' "
                    'ORDER BY id DESC'):
                event = Store._event(event_row)
                if event['payload'].get('requirement_id') == requirement['id']:
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
                    db, 'requirement.verified', {
                        'requirement_id': requirement['id'],
                        'evidence': evidence,
                        'status': AWAITING_ACCEPTANCE,
                        'migrated_from_event_id': accepted['id'],
                        'migration_reason': 'legacy coordinator acceptance required '
                                            'the human final gate',
                    }, source_key=source_key, acknowledged=True)
            db.execute(
                "UPDATE requirements SET status=?, target_status=NULL "
                "WHERE id=? AND status='accepted'",
                (AWAITING_ACCEPTANCE, requirement['id']))

    @staticmethod
    def _require_title_columns(db):
        for table in ('drafts', 'requirements'):
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
        for table in ('drafts', 'requirements'):
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
    def _stable(requirement):
        return (requirement['status'] in STABLE_STATUSES
                and requirement.get('target_status') is None)

    def _require_replacement_bases(self, db, replacement_ids):
        bases = []
        for identifier in replacement_ids:
            try:
                base = self._get(db, 'requirements', identifier)
            except KeyError:
                if db.execute('SELECT 1 FROM drafts WHERE id = ?', (identifier,)).fetchone():
                    raise ValueError(
                        f'replacement_of {identifier} is a draft, not a confirmed requirement') from None
                raise ValueError(
                    f'replacement_of {identifier} does not reference an existing requirement') from None
            if not self._stable(base):
                raise ValueError(
                    f'replacement_of {identifier} is {base["status"]}; '
                    'replacement bases must be stable active or paused requirements') from None
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
        cancellation['requirement_ids'] = json.loads(cancellation['requirement_ids'])
        return cancellation

    def create_cancellation(self, requirement_ids, reason=None, summary=None):
        """Create an immutable cancellation proposal without changing requirements."""
        ids = list(dict.fromkeys(requirement_ids))
        if not ids or any(not isinstance(i, str) or not i.strip() for i in ids):
            raise ValueError('requirement_ids must contain nonempty IDs')
        reason = self._optional_text(reason)
        summary = self._optional_text(summary)
        identifier = uuid4().hex
        with self._transaction() as db:
            rows = [self._get(db, 'requirements', i) for i in ids]
            if any(not self._stable(row) for row in rows):
                raise ValueError('all requirements must be stable active or paused')
            db.execute('INSERT INTO cancellations VALUES (?, ?, ?, ?, NULL)',
                       (identifier, json.dumps(ids), reason, summary))
            return self._cancellation(self._get(db, 'cancellations', identifier))

    def get_cancellation(self, cancellation_id):
        with self._transaction() as db:
            return self._cancellation(self._get(db, 'cancellations', cancellation_id))

    def create_requirement_command(self, action, requirement_id, reason, summary=None):
        """Create an immutable pause/resume proposal without changing status."""
        if action not in {'pause', 'resume'}:
            raise ValueError('unknown requirement command')
        reason = self._text(reason)
        summary = self._optional_text(summary)
        required = 'active' if action == 'pause' else 'paused'
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if not self._stable(requirement) or requirement['status'] != required:
                raise ValueError(f'{action} requires a stable {required} requirement')
            existing = db.execute(
                'SELECT * FROM requirement_commands WHERE action=? AND requirement_id=? '
                'AND reason=? AND summary=? AND confirmation_reference IS NULL '
                'ORDER BY rowid LIMIT 1',
                (action, requirement_id, reason, summary)).fetchone()
            if existing:
                return dict(existing)
            identifier = uuid4().hex
            db.execute('INSERT INTO requirement_commands VALUES (?, ?, ?, ?, ?, NULL)',
                       (identifier, action, requirement_id, reason, summary))
            return dict(db.execute('SELECT * FROM requirement_commands WHERE id=?',
                                   (identifier,)).fetchone())

    def get_requirement_command(self, command_id):
        with self._transaction() as db:
            return self._get(db, 'requirement_commands', command_id)

    def confirm_requirement_command(self, command_id, confirmation_reference):
        """Request one exact pause/resume transition atomically and idempotently."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            command = self._get(db, 'requirement_commands', command_id)
            requirement = self._get(db, 'requirements', command['requirement_id'])
            if command['confirmation_reference'] is not None:
                return requirement
            action = command['action']
            target = 'paused' if action == 'pause' else 'active'
            required = 'active' if action == 'pause' else 'paused'
            if not self._stable(requirement) or requirement['status'] != required:
                raise ValueError(f'{action} requires a stable {required} requirement')
            db.execute('UPDATE requirements SET target_status=?,control_version=control_version+1 WHERE id=?',
                       (target, requirement['id']))
            self._add_event(db, 'requirement.transition_requested', {
                'requirement_id': requirement['id'], 'command_id': command_id,
                'from_status': requirement['status'], 'target_status': target,
                'reason': command['reason'], 'summary': command['summary'],
                'confirmation_reference': confirmation_reference})
            requirement = self._get(db, 'requirements', requirement['id'])
            db.execute('UPDATE requirement_commands SET confirmation_reference=? WHERE id=?',
                       (confirmation_reference, command_id))
            return requirement

    def create_human_decision(self, requirement_id, action, feedback=None):
        """Prepare an immutable human accept/request-changes proposal."""
        if action not in {'accept', 'request_changes'}:
            raise ValueError('unknown human acceptance decision')
        feedback = self._optional_text(feedback)
        if action == 'request_changes' and feedback is None:
            raise ValueError('request_changes requires feedback')
        if action == 'accept' and feedback is not None:
            raise ValueError('accept does not accept feedback; use request_changes')
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if (requirement['status'] != AWAITING_ACCEPTANCE
                    or requirement['target_status'] is not None):
                raise ValueError('requirement is not awaiting human acceptance')
            if self._latest_verification(db, requirement_id) is None:
                raise ValueError('coordinator verification evidence is missing')
            stored_action = ('human_accept' if action == 'accept'
                             else 'human_request_changes')
            existing = db.execute(
                'SELECT * FROM requirement_commands WHERE action=? AND requirement_id=? '
                'AND reason IS ? AND confirmation_reference IS NULL '
                'ORDER BY rowid LIMIT 1',
                (stored_action, requirement_id, feedback)).fetchone()
            verification = self._latest_verification(db, requirement_id)
            if existing:
                binding = db.execute('SELECT * FROM command_bindings WHERE command_id=?', (existing['id'],)).fetchone()
                if binding and binding['generation'] == requirement['generation'] and binding['verification_id'] == verification['id']:
                    return dict(existing)
            identifier = uuid4().hex
            db.execute('INSERT INTO command_bindings VALUES(?,?,?)', (identifier, requirement['generation'], verification['id']))
            db.execute('INSERT INTO requirement_commands VALUES (?, ?, ?, ?, ?, NULL)',
                       (identifier, stored_action, requirement_id, feedback, None))
            return dict(db.execute('SELECT * FROM requirement_commands WHERE id=?',
                                   (identifier,)).fetchone())

    def latest_verification(self, requirement_id):
        with self._transaction() as db:
            return self._latest_verification(db, requirement_id)

    def confirm_human_decision(self, command_id, confirmation_reference):
        """Commit one exact human acceptance decision idempotently."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            command = self._get(db, 'requirement_commands', command_id)
            if command['action'] not in {'human_accept', 'human_request_changes'}:
                raise ValueError('not a human acceptance decision')
            requirement = self._get(db, 'requirements', command['requirement_id'])
            if command['confirmation_reference'] is not None:
                return requirement
            if (requirement['status'] != AWAITING_ACCEPTANCE
                    or requirement['target_status'] is not None):
                raise ValueError('requirement is not awaiting human acceptance')
            verification = self._latest_verification(db, requirement['id'])
            if verification is None:
                raise ValueError('coordinator verification evidence is missing')
            binding = db.execute('SELECT * FROM command_bindings WHERE command_id=?', (command_id,)).fetchone()
            if not binding or binding['generation'] != requirement['generation'] or binding['verification_id'] != verification['id']:
                raise RuntimeError('acceptance proposal is stale; prepare a new decision')
            if command['action'] == 'human_accept':
                db.execute("UPDATE requirements SET status='accepted', target_status=NULL,control_version=control_version+1 "
                           'WHERE id=?', (requirement['id'],))
                self._add_event(
                    db, 'requirement.accepted',
                    {'requirement_id': requirement['id'],
                     'evidence': verification['payload']['evidence'],
                     'verification_event_id': verification['id'],
                     'confirmation_reference': confirmation_reference,
                     'command_id': command_id},
                    acknowledged=True)
            else:
                db.execute("UPDATE requirements SET status='active', target_status=NULL,control_version=control_version+1 "
                           'WHERE id=?', (requirement['id'],))
                self._add_event(
                    db, 'requirement.changes_requested',
                    {'requirement_id': requirement['id'],
                     'feedback': command['reason'],
                     'verification_event_id': verification['id'],
                     'confirmation_reference': confirmation_reference,
                     'command_id': command_id})
            db.execute('UPDATE requirement_commands SET confirmation_reference=? WHERE id=?',
                       (confirmation_reference, command_id))
            return self._get(db, 'requirements', requirement['id'])

    def get_requirement(self, requirement_id):
        with self._transaction() as db:
            return self._get(db, 'requirements', requirement_id)

    @staticmethod
    def _note_body(value):
        body = Store._text(value)
        if len(body) > NOTE_MAX_CHARS:
            raise ValueError(f'note body must be at most {NOTE_MAX_CHARS} characters')
        return body

    @staticmethod
    def _require_note_mutable(requirement):
        if not Store._stable(requirement):
            raise ValueError(
                'notes can be changed only while the requirement is stable active or paused')

    def _get_note(self, db, requirement_id, note_id):
        row = db.execute(
            'SELECT * FROM requirement_notes WHERE id=? AND requirement_id=?',
            (note_id, requirement_id)).fetchone()
        if row is None:
            raise KeyError(note_id)
        return dict(row)

    def add_note(self, requirement_id, body, *, actor='ancor-coordinator'):
        body = self._note_body(body)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            self._require_note_mutable(requirement)
            now = int(time.time())
            identifier = uuid4().hex
            db.execute(
                'INSERT INTO requirement_notes '
                '(id, requirement_id, body, revision, created_at, updated_at, deleted_at) '
                'VALUES (?, ?, ?, 1, ?, ?, NULL)',
                (identifier, requirement_id, body, now, now))
            note = self._get_note(db, requirement_id, identifier)
            self._note_history(db, note, actor)
            return note

    def get_note(self, requirement_id, note_id):
        with self._transaction() as db:
            self._get(db, 'requirements', requirement_id)
            return self._get_note(db, requirement_id, note_id)

    def list_notes(self, requirement_id, limit=50, include_deleted=False):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit must be an integer between 1 and 200')
        if type(include_deleted) is not bool:
            raise ValueError('include_deleted must be a boolean')
        with self._transaction() as db:
            self._get(db, 'requirements', requirement_id)
            where = 'requirement_id=?' + ('' if include_deleted else ' AND deleted_at IS NULL')
            total = db.execute(
                f'SELECT COUNT(*) FROM requirement_notes WHERE {where}',
                (requirement_id,)).fetchone()[0]
            rows = db.execute(
                f'SELECT * FROM requirement_notes WHERE {where} '
                'ORDER BY updated_at DESC, rowid DESC LIMIT ?',
                (requirement_id, limit)).fetchall()
            return {'items': [dict(row) for row in rows], 'count': total}

    def update_note(self, requirement_id, note_id, body, *, expected_revision=None, actor='ancor-coordinator'):
        body = self._note_body(body)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            self._require_note_mutable(requirement)
            note = self._get_note(db, requirement_id, note_id)
            if expected_revision is not None and note['revision'] != expected_revision:
                raise RuntimeError('note version changed')
            if note['deleted_at'] is not None:
                raise ValueError('deleted note cannot be updated')
            db.execute(
                'UPDATE requirement_notes SET body=?, revision=revision+1, updated_at=? '
                'WHERE id=? AND requirement_id=?',
                (body, int(time.time()), note_id, requirement_id))
            note = self._get_note(db, requirement_id, note_id)
            self._note_history(db, note, actor)
            return note

    def delete_note(self, requirement_id, note_id, *, expected_revision=None, actor='ancor-coordinator'):
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            self._require_note_mutable(requirement)
            note = self._get_note(db, requirement_id, note_id)
            if expected_revision is not None and note['revision'] != expected_revision:
                raise RuntimeError('note version changed')
            if note['deleted_at'] is not None:
                return note
            now = int(time.time())
            db.execute(
                'UPDATE requirement_notes SET deleted_at=?, revision=revision+1, updated_at=? '
                'WHERE id=? AND requirement_id=?',
                (now, now, note_id, requirement_id))
            note = self._get_note(db, requirement_id, note_id)
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
        """Stage a confirmed draft and any replaced requirements for transition."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            existing = db.execute('SELECT * FROM requirements WHERE draft_id = ?',
                                  (draft_id,)).fetchone()
            if existing:
                return dict(existing)
            draft = self._get(db, 'drafts', draft_id)
            replacement_ids = ([draft['replacement_of']] if draft['replacement_of'] is not None else [])
            if draft['replacement_of'] and draft['replacement_of'].startswith('['):
                replacement_ids = json.loads(draft['replacement_of'])
            bases = self._require_replacement_bases(db, replacement_ids)
            for base in bases:
                db.execute("UPDATE requirements SET target_status = 'invalidated',control_version=control_version+1 WHERE id = ?",
                           (base['id'],))
            identifier = uuid4().hex
            status = 'pending' if bases else 'active'
            target_status = 'active' if bases else None
            db.execute('INSERT INTO requirements '
                       '(id, draft_id, title, brief, replacement_of, '
                       'confirmation_reference, status, target_status) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (identifier, draft_id, draft['title'], draft['brief'],
                        draft['replacement_of'], confirmation_reference, status, target_status))
            for base in bases:
                self._add_event(db, 'requirement.transition_requested', {
                    'requirement_id': base['id'], 'replacement_id': identifier,
                    'from_status': base['status'], 'target_status': 'invalidated'})
            if bases:
                self._add_event(db, 'requirement.staged', {
                    'requirement_id': identifier, 'draft_id': draft_id,
                    'replacement_of': draft['replacement_of'],
                    'target_status': 'active'})
            else:
                self._add_event(db, 'requirement.activated', {
                    'requirement_id': identifier, 'draft_id': draft_id,
                    'from_status': None, 'target_status': 'active'})
            return self._get(db, 'requirements', identifier)

    def link_task(self, requirement_id, task_id):
        self._text(task_id)
        with self._transaction() as db:
            self._get(db, 'requirements', requirement_id)
            db.execute('INSERT OR IGNORE INTO task_links VALUES (?, ?)',
                       (requirement_id, task_id))

    def snapshot(self):
        with self._transaction() as db:
            return {
                'drafts': [dict(row) for row in db.execute('SELECT * FROM drafts ORDER BY rowid')],
                'requirements': [dict(row) for row in db.execute(
                    'SELECT * FROM requirements ORDER BY rowid')],
                'task_links': [dict(row) for row in db.execute(
                    'SELECT * FROM task_links ORDER BY requirement_id, task_id')],
                'pending_events': [self._event(row) for row in db.execute(
                    'SELECT * FROM events WHERE acknowledged = 0 ORDER BY id')],
            }

    def acceptances(self):
        """Retain coordinator verification and final human acceptance evidence."""
        with self._transaction() as db:
            return [self._event(row) for row in db.execute(
                "SELECT * FROM events WHERE kind IN "
                "('requirement.verified', 'requirement.changes_requested', "
                "'requirement.accepted') ORDER BY id")]

    def requirement_transitions(self, requirement_id=None):
        """Retain requirement transition history after its events are handled."""
        with self._transaction() as db:
            events = [self._event(row) for row in db.execute(
                "SELECT * FROM events WHERE kind IN "
                "('requirement.staged', 'requirement.transition_requested', "
                "'requirement.activated', 'requirement.paused', 'requirement.resumed', "
                "'requirement.verified', 'requirement.changes_requested', "
                "'requirement.accepted', 'requirement.cancelled', "
                "'requirement.invalidated') ORDER BY id")]
        if requirement_id is not None:
            events = [event for event in events
                      if event['payload'].get('requirement_id') == requirement_id]
        return events

    @staticmethod
    def _replacement_ids(value):
        if value is None:
            return []
        if isinstance(value, str) and value.startswith('['):
            return json.loads(value)
        return [value]

    def _transition_event(self, db, requirement, target):
        requirement_id = requirement['id']
        payload = {
            'requirement_id': requirement_id,
            'from_status': requirement['status'],
            'target_status': target,
        }
        action = {'paused': 'pause', 'active': 'resume'}.get(target)
        requested = None
        if action is not None:
            command = db.execute(
                'SELECT * FROM requirement_commands WHERE requirement_id=? AND action=? '
                'AND confirmation_reference IS NOT NULL ORDER BY rowid DESC LIMIT 1',
                (requirement_id, action)).fetchone()
            if command:
                payload.update(command_id=command['id'], reason=command['reason'],
                               summary=command['summary'],
                               confirmation_reference=command['confirmation_reference'])
            else:
                requested = self._latest_transition_request(db, requirement_id, target)
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
                identifiers = json.loads(row['requirement_ids'])
                if requirement_id in identifiers:
                    payload.update(reason=row['reason'], summary=row['summary'],
                                   batch_requirement_ids=identifiers,
                                   confirmation_reference=row['confirmation_reference'])
                    found = True
                    break
            if not found:
                requested = self._latest_transition_request(db, requirement_id, target)
                if requested:
                    payload.update({
                        key: requested.get(key) for key in
                        ('reason', 'batch_requirement_ids', 'confirmation_reference')
                        if requested.get(key) is not None})
        if target == 'invalidated':
            for successor in db.execute(
                    "SELECT * FROM requirements WHERE status='pending' "
                    "AND target_status='active' ORDER BY rowid"):
                if requirement_id in self._replacement_ids(successor['replacement_of']):
                    payload['replacement_id'] = successor['id']
                    break
        if target == 'active' and requirement['status'] == 'pending':
            replacement_ids = self._replacement_ids(requirement['replacement_of'])
            payload['replacement_of'] = requirement['replacement_of']
            if replacement_ids:
                payload['invalidated_requirement_ids'] = replacement_ids
        kind = ('requirement.paused' if target == 'paused' else
                'requirement.resumed' if target == 'active' and requirement['status'] == 'paused' else
                'requirement.activated' if target == 'active' else
                'requirement.cancelled' if target == 'cancelled' else
                'requirement.invalidated')
        requested = self._latest_transition_request(db, requirement_id, target)
        if requested and requested.get('change_id'):
            for key in ('command_id', 'reason', 'summary', 'confirmation_reference'):
                payload.pop(key, None)
            payload.update({key: requested[key] for key in ('change_id', 'reason', 'confirmation_reference') if key in requested})
        return kind, payload

    @staticmethod
    def _latest_transition_request(db, requirement_id, target):
        for row in db.execute(
                "SELECT payload FROM events WHERE kind='requirement.transition_requested' "
                'ORDER BY id DESC'):
            payload = json.loads(row['payload'])
            if (payload.get('requirement_id') == requirement_id
                    and payload.get('target_status') == target):
                return payload
        return None

    def _finalize_one(self, db, requirement):
        target = requirement.get('target_status')
        if target is None:
            return requirement
        if target not in TRANSITION_TARGETS:
            raise RuntimeError(f'unknown target requirement status {target!r}')
        if target == 'active' and requirement['status'] == 'pending':
            replacement_ids = self._replacement_ids(requirement['replacement_of'])
            if any(self._get(db, 'requirements', identifier)['status'] != 'invalidated'
                   for identifier in replacement_ids):
                raise RuntimeError('replacement bases are not yet invalidated')
        kind, payload = self._transition_event(db, requirement, target)
        cursor = db.execute(
            'UPDATE requirements SET status=?, target_status=NULL, control_version=control_version+1 '
            'WHERE id=? AND status=? AND target_status=?',
            (target, requirement['id'], requirement['status'], target))
        if cursor.rowcount != 1:
            raise RuntimeError('requirement transition changed concurrently')
        self._add_event(db, kind, payload)
        return self._get(db, 'requirements', requirement['id'])

    def submit_acceptance(self, requirement_id, evidence, *, generation=None):
        """Record coordinator verification and wait for the human's final decision."""
        self._text(evidence)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['status'] != 'active' or requirement['target_status'] is not None:
                raise ValueError('requirement is not stably active')
            if generation is not None and requirement['generation'] != generation:
                raise RuntimeError('requirement changed during verification')
            if requirement['content_mode'] == 'points':
                self._check_verification(db, requirement, evidence)
            event = self._add_event(
                db, 'requirement.verified',
                {'requirement_id': requirement_id, 'evidence': evidence,
                 'generation': requirement['generation'], 'status': AWAITING_ACCEPTANCE})
            db.execute('UPDATE requirements SET status=?,verification_id=? WHERE id=?',
                       (AWAITING_ACCEPTANCE, event['id'], requirement_id))
            # Verification itself needs no coordinator follow-up. Keeping this
            # audit event acknowledged prevents a redundant wake while the
            # requirement waits for the human gate.
            db.execute('UPDATE events SET acknowledged=1 WHERE id=?', (event['id'],))
            return self._get(db, 'requirements', requirement_id)

    @staticmethod
    def _latest_verification(db, requirement_id):
        for row in db.execute(
                "SELECT * FROM events WHERE kind='requirement.verified' "
                'ORDER BY id DESC'):
            event = Store._event(row)
            if event['payload'].get('requirement_id') == requirement_id:
                return event
        return None

    def accept(self, requirement_id, confirmation_reference=None, feedback=None):
        """Commit the human's final acceptance of coordinator-verified work."""
        if confirmation_reference is not None:
            self._text(confirmation_reference)
        feedback = self._optional_text(feedback)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if (requirement['status'] != AWAITING_ACCEPTANCE
                    or requirement['target_status'] is not None):
                raise ValueError('requirement is not awaiting human acceptance')
            verification = self._latest_verification(db, requirement_id)
            if verification is None:
                raise ValueError('coordinator verification evidence is missing')
            db.execute("UPDATE requirements SET status='accepted', target_status=NULL,control_version=control_version+1 "
                       'WHERE id=?', (requirement_id,))
            self._add_event(
                db, 'requirement.accepted',
                {'requirement_id': requirement_id,
                 'evidence': verification['payload']['evidence'],
                 'verification_event_id': verification['id'],
                 'confirmation_reference': confirmation_reference,
                 **({'feedback': feedback} if feedback else {})},
                acknowledged=True)
            return self._get(db, 'requirements', requirement_id)

    def request_changes(self, requirement_id, feedback, confirmation_reference=None):
        """Return verified work to active coordination with exact human feedback."""
        feedback = self._text(feedback)
        if confirmation_reference is not None:
            self._text(confirmation_reference)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if (requirement['status'] != AWAITING_ACCEPTANCE
                    or requirement['target_status'] is not None):
                raise ValueError('requirement is not awaiting human acceptance')
            verification = self._latest_verification(db, requirement_id)
            if verification is None:
                raise ValueError('coordinator verification evidence is missing')
            db.execute("UPDATE requirements SET status='active', target_status=NULL,control_version=control_version+1 "
                       'WHERE id=?', (requirement_id,))
            self._add_event(
                db, 'requirement.changes_requested',
                {'requirement_id': requirement_id, 'feedback': feedback,
                 'verification_event_id': verification['id'],
                 'confirmation_reference': confirmation_reference})
            return self._get(db, 'requirements', requirement_id)

    def _request_cancel(self, db, requirement_ids, reason):
        ids = list(dict.fromkeys(requirement_ids))
        if not ids or any(not isinstance(i, str) or not i.strip() for i in ids):
            raise ValueError('requirement_ids must contain nonempty IDs')
        rows = [self._get(db, 'requirements', i) for i in ids]
        if any(not self._stable(row) for row in rows):
            raise ValueError('all requirements must be stable active or paused')
        for row in rows:
            db.execute("UPDATE requirements SET target_status = 'cancelled',control_version=control_version+1 WHERE id = ?",
                       (row['id'],))
            self._add_event(db, 'requirement.transition_requested', {
                'requirement_id': row['id'], 'from_status': row['status'],
                'target_status': 'cancelled', 'reason': reason,
                'batch_requirement_ids': ids})
        return [self._get(db, 'requirements', i) for i in ids]

    def cancel(self, requirement_ids, reason):
        """Request cancellation of stable requirements; advance commits it."""
        self._text(reason)
        with self._transaction() as db:
            return self._request_cancel(db, requirement_ids, reason)

    def confirm_cancellation(self, cancellation_id, confirmation_reference):
        """Consume one exact cancellation confirmation idempotently and atomically."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            cancellation = self._get(db, 'cancellations', cancellation_id)
            ids = json.loads(cancellation['requirement_ids'])
            if cancellation['confirmation_reference'] is not None:
                return [self._get(db, 'requirements', i) for i in ids]
            rows = self._request_cancel(
                db, ids, cancellation['reason'] or 'User confirmed cancellation')
            db.execute('UPDATE cancellations SET confirmation_reference = ? WHERE id = ?',
                       (confirmation_reference, cancellation_id))
            return rows

    def finalize_transition(self, requirement_id):
        """Commit a verified transition and activate any replacement made ready by it."""
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['target_status'] is None:
                return {'requirement': requirement, 'activated': []}
            target = requirement['target_status']
            finalized = self._finalize_one(db, requirement)
            activated = []
            if target == 'invalidated':
                for successor in db.execute(
                        "SELECT * FROM requirements WHERE status='pending' "
                        "AND target_status='active' ORDER BY rowid").fetchall():
                    successor = dict(successor)
                    replacement_ids = self._replacement_ids(successor['replacement_of'])
                    if requirement_id not in replacement_ids:
                        continue
                    if all(self._get(db, 'requirements', identifier)['status'] == 'invalidated'
                           for identifier in replacement_ids):
                        activated.append(self._finalize_one(db, successor))
            return {'requirement': finalized, 'activated': activated}

    def reject(self, requirement_id, reason):
        self._text(reason)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['status'] != 'active' or requirement['target_status'] is not None:
                raise ValueError('requirement is not stably active')
            self._add_event(db, 'requirement.rejected',
                            {'requirement_id': requirement_id, 'reason': reason})
            return requirement


    def associate_task(self, task_id, requirement_id, reason):
        self._text(task_id); self._text(reason)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['status'] != 'active' or requirement['target_status'] is not None:
                raise ValueError('association target must be stably active')
            db.execute('INSERT OR IGNORE INTO task_links VALUES (?, ?)', (requirement_id, task_id))
            self._add_event(db, 'task.associated', {'task_id': task_id, 'requirement_id': requirement_id, 'reason': reason})
            return {'task_id': task_id, 'requirement_id': requirement_id, 'reason': reason}
