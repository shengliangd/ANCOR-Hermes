"""Durable requirements, separate from the native task board.

Confirmation references are integration-supplied audit references, not proof
of human consent. Returned dictionaries are detached copies, never live state.
"""
from contextlib import contextmanager
import json
import sqlite3
from uuid import uuid4

from .views import TITLE_MAX_CHARS


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self._transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, brief TEXT NOT NULL,
                    acceptance_criteria TEXT NOT NULL, replacement_of TEXT,
                    summary TEXT);
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
                    brief TEXT NOT NULL, acceptance_criteria TEXT NOT NULL,
                    replacement_of TEXT, confirmation_reference TEXT NOT NULL,
                    status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_links (
                    requirement_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    PRIMARY KEY (requirement_id, task_id));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, source_key TEXT UNIQUE,
                    acknowledged INTEGER NOT NULL DEFAULT 0);
            ''')
            self._require_title_columns(db)
            self._ensure_columns(db, 'drafts', {'summary': 'TEXT'})

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
            if base['status'] not in {'active', 'paused'}:
                raise ValueError(
                    f'replacement_of {identifier} is {base["status"]}; '
                    'replacement bases must be active or paused') from None
            bases.append(base)
        return bases

    def create_draft(self, brief, acceptance_criteria, replacement_of=None,
                     summary=None, *, title):
        title = self._title(title)
        self._text(brief)
        self._text(acceptance_criteria)
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
            db.execute('INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)',
                       (identifier, title, brief, acceptance_criteria,
                        replacement_of, summary))
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
            if any(row['status'] not in {'active', 'paused'} for row in rows):
                raise ValueError('all requirements must be active or paused')
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
            if requirement['status'] != required:
                raise ValueError(f'{action} requires a {required} requirement')
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
        """Consume one exact pause/resume confirmation atomically and idempotently."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            command = self._get(db, 'requirement_commands', command_id)
            requirement = self._get(db, 'requirements', command['requirement_id'])
            if command['confirmation_reference'] is not None:
                return requirement
            action = command['action']
            target = 'paused' if action == 'pause' else 'active'
            if requirement['status'] != target:
                required = 'active' if action == 'pause' else 'paused'
                if requirement['status'] != required:
                    raise ValueError(f'{action} requires a {required} requirement')
                db.execute('UPDATE requirements SET status=? WHERE id=?',
                           (target, requirement['id']))
                self._add_event(
                    db, 'requirement.' + ('paused' if action == 'pause' else 'resumed'), {
                        'requirement_id': requirement['id'], 'command_id': command_id,
                        'reason': command['reason'], 'summary': command['summary'],
                        'confirmation_reference': confirmation_reference},
                    source_key='requirement.' + action + ':' + command_id)
                requirement = self._get(db, 'requirements', requirement['id'])
            db.execute('UPDATE requirement_commands SET confirmation_reference=? WHERE id=?',
                       (confirmation_reference, command_id))
            return requirement

    def get_requirement(self, requirement_id):
        with self._transaction() as db:
            return self._get(db, 'requirements', requirement_id)

    @staticmethod
    def _event(row):
        event = dict(row)
        event['payload'] = json.loads(event['payload'])
        event['acknowledged'] = bool(event['acknowledged'])
        return event

    def _add_event(self, db, kind, payload, source_key=None):
        self._text(kind)
        if source_key is not None:
            self._text(source_key)
            existing = db.execute('SELECT * FROM events WHERE source_key = ?',
                                  (source_key,)).fetchone()
            if existing:
                return self._event(existing)
        cursor = db.execute('INSERT INTO events (kind, payload, source_key) VALUES (?, ?, ?)',
                            (kind, json.dumps(payload), source_key))
        return self._event(db.execute('SELECT * FROM events WHERE id = ?',
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
                db.execute("UPDATE requirements SET status = 'invalidated' WHERE id = ?", (base['id'],))
            identifier = uuid4().hex
            db.execute('INSERT INTO requirements VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (identifier, draft_id, draft['title'], draft['brief'],
                        draft['acceptance_criteria'], draft['replacement_of'],
                        confirmation_reference, 'active'))
            for base in bases:
                self._add_event(db, 'requirement.invalidated',
                                {'requirement_id': base['id'], 'replacement_id': identifier})
            self._add_event(db, 'requirement.confirmed', {'requirement_id': identifier})
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
        """Retain accepted evidence even after its coordination event is handled."""
        with self._transaction() as db:
            return [self._event(row) for row in db.execute(
                "SELECT * FROM events WHERE kind = 'requirement.accepted' ORDER BY id")]

    def requirement_transitions(self, requirement_id=None):
        """Retain pause/resume history even after its coordination event is handled."""
        with self._transaction() as db:
            events = [self._event(row) for row in db.execute(
                "SELECT * FROM events WHERE kind IN "
                "('requirement.paused', 'requirement.resumed') ORDER BY id")]
        if requirement_id is not None:
            events = [event for event in events
                      if event['payload'].get('requirement_id') == requirement_id]
        return events

    def accept(self, requirement_id, evidence):
        self._text(evidence)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['status'] != 'active':
                raise ValueError('requirement is not active')
            db.execute("UPDATE requirements SET status = 'accepted' WHERE id = ?",
                       (requirement_id,))
            self._add_event(db, 'requirement.accepted',
                            {'requirement_id': requirement_id, 'evidence': evidence})
            return self._get(db, 'requirements', requirement_id)

    def _cancel(self, db, requirement_ids, reason):
        ids = list(dict.fromkeys(requirement_ids))
        if not ids or any(not isinstance(i, str) or not i.strip() for i in ids):
            raise ValueError('requirement_ids must contain nonempty IDs')
        rows = [self._get(db, 'requirements', i) for i in ids]
        if any(row['status'] not in {'active', 'paused'} for row in rows):
            raise ValueError('all requirements must be active or paused')
        for row in rows:
            db.execute("UPDATE requirements SET status = 'cancelled' WHERE id = ?", (row['id'],))
            self._add_event(db, 'requirement.cancelled', {
                'requirement_id': row['id'], 'reason': reason,
                'batch_requirement_ids': ids})
        return [self._get(db, 'requirements', i) for i in ids]

    def cancel(self, requirement_ids, reason):
        """Cancel one or more active requirements atomically."""
        self._text(reason)
        with self._transaction() as db:
            return self._cancel(db, requirement_ids, reason)

    def confirm_cancellation(self, cancellation_id, confirmation_reference):
        """Consume one exact cancellation confirmation idempotently and atomically."""
        self._text(confirmation_reference)
        with self._transaction() as db:
            cancellation = self._get(db, 'cancellations', cancellation_id)
            ids = json.loads(cancellation['requirement_ids'])
            if cancellation['confirmation_reference'] is not None:
                return [self._get(db, 'requirements', i) for i in ids]
            rows = self._cancel(db, ids, cancellation['reason'] or 'User confirmed cancellation')
            db.execute('UPDATE cancellations SET confirmation_reference = ? WHERE id = ?',
                       (confirmation_reference, cancellation_id))
            return rows

    def reject(self, requirement_id, reason):
        self._text(reason)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['status'] != 'active':
                raise ValueError('requirement is not active')
            self._add_event(db, 'requirement.rejected',
                            {'requirement_id': requirement_id, 'reason': reason})
            return requirement


    def associate_task(self, task_id, requirement_id, reason):
        self._text(task_id); self._text(reason)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['status'] != 'active':
                raise ValueError('association target must be active')
            db.execute('INSERT OR IGNORE INTO task_links VALUES (?, ?)', (requirement_id, task_id))
            self._add_event(db, 'task.associated', {'task_id': task_id, 'requirement_id': requirement_id, 'reason': reason})
            return {'task_id': task_id, 'requirement_id': requirement_id, 'reason': reason}
