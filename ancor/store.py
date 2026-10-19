"""Durable requirements, separate from the native task board.

Confirmation references are integration-supplied audit references, not proof
of human consent. Returned dictionaries are detached copies, never live state.
"""
from contextlib import contextmanager
import json
import sqlite3
from uuid import uuid4


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self._transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, brief TEXT NOT NULL,
                    acceptance_criteria TEXT NOT NULL, replacement_of TEXT);
                CREATE TABLE IF NOT EXISTS requirements (
                    id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL,
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
    def _get(db, table, identifier):
        row = db.execute(f'SELECT * FROM {table} WHERE id = ?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return dict(row)

    def create_draft(self, brief, acceptance_criteria, replacement_of=None):
        self._text(brief)
        self._text(acceptance_criteria)
        if isinstance(replacement_of, list):
            if not replacement_of or any(not isinstance(i, str) or not i.strip() for i in replacement_of):
                raise ValueError('replacement_of list must contain nonempty IDs')
            replacement_of = json.dumps(list(dict.fromkeys(replacement_of)))
        else:
            replacement_of = self._optional_text(replacement_of)
        identifier = uuid4().hex
        with self._transaction() as db:
            db.execute('INSERT INTO drafts VALUES (?, ?, ?, ?)',
                       (identifier, brief, acceptance_criteria, replacement_of))
            return self._get(db, 'drafts', identifier)

    def get_draft(self, draft_id):
        with self._transaction() as db:
            return self._get(db, 'drafts', draft_id)

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
            bases = [self._get(db, 'requirements', i) for i in replacement_ids]
            if any(base['status'] != 'active' for base in bases):
                raise ValueError('all replacement bases must be active')
            for base in bases:
                db.execute("UPDATE requirements SET status = 'invalidated' WHERE id = ?", (base['id'],))
            identifier = uuid4().hex
            db.execute('INSERT INTO requirements VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (identifier, draft_id, draft['brief'], draft['acceptance_criteria'],
                        draft['replacement_of'], confirmation_reference, 'active'))
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

    def cancel_many(self, requirement_ids, reason):
        self._text(reason)
        ids = list(dict.fromkeys(requirement_ids))
        if not ids or any(not isinstance(i, str) or not i.strip() for i in ids):
            raise ValueError('requirement_ids must contain nonempty IDs')
        with self._transaction() as db:
            rows = [self._get(db, 'requirements', i) for i in ids]
            if any(row['status'] != 'active' for row in rows):
                raise ValueError('all requirements must be active')
            for row in rows:
                db.execute("UPDATE requirements SET status = 'cancelled' WHERE id = ?", (row['id'],))
                self._add_event(db, 'requirement.cancelled', {'requirement_id': row['id'], 'reason': reason, 'batch_requirement_ids': ids})
            return [self._get(db, 'requirements', i) for i in ids]

    def reject(self, requirement_id, reason):
        self._text(reason)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['status'] != 'active':
                raise ValueError('requirement is not active')
            self._add_event(db, 'requirement.rejected',
                            {'requirement_id': requirement_id, 'reason': reason})
            return requirement

    def cancel(self, requirement_id, reason):
        """Cancel an active requirement and emit a durable coordinator input."""
        self._text(reason)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['status'] != 'active':
                raise ValueError('requirement is not active')
            db.execute("UPDATE requirements SET status = 'cancelled' WHERE id = ?",
                       (requirement_id,))
            self._add_event(db, 'requirement.cancelled',
                            {'requirement_id': requirement_id, 'reason': reason})
            return self._get(db, 'requirements', requirement_id)

    def associate_task(self, task_id, requirement_id, reason):
        self._text(task_id); self._text(reason)
        with self._transaction() as db:
            requirement = self._get(db, 'requirements', requirement_id)
            if requirement['status'] != 'active':
                raise ValueError('association target must be active')
            db.execute('INSERT OR IGNORE INTO task_links VALUES (?, ?)', (requirement_id, task_id))
            self._add_event(db, 'task.associated', {'task_id': task_id, 'requirement_id': requirement_id, 'reason': reason})
            return {'task_id': task_id, 'requirement_id': requirement_id, 'reason': reason}
