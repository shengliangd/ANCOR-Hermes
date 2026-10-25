"""Offline-only translation of the pre-request vocabulary.

Runtime APIs have no legacy aliases. Text authored by humans is never rewritten;
only schema identifiers and machine-owned JSON keys/references are translated.
"""
import json
import re
import sqlite3
import time

OLD = 'requirement'
NEW = 'request'


def renamed(value):
    return value.replace(OLD, NEW).replace('Requirement', 'Request').replace('REQUIREMENT', 'REQUEST')


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def has_legacy_schema(db):
    return OLD + 's' in tables(db)


def translate_json(value):
    if isinstance(value, dict):
        return {renamed(k): translate_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [translate_json(v) for v in value]
    # Only machine-owned reference prefixes; ordinary text is verbatim.
    if isinstance(value, str) and value.startswith((OLD + '.', OLD + '.response:')):
        return NEW + value[len(OLD):]
    return value


def migrate_schema(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN IMMEDIATE')
        for table in sorted(tables(db)):
            target = renamed(table)
            if table != target:
                if target in tables(db):
                    raise ValueError('mixed old/new schema; refusing to merge identities')
                db.execute(f'ALTER TABLE {quote(table)} RENAME TO {quote(target)}')
        for table in sorted(tables(db)):
            for row in list(db.execute(f'PRAGMA table_info({quote(table)})')):
                column, target = row['name'], renamed(row['name'])
                if column != target:
                    db.execute(f'ALTER TABLE {quote(table)} RENAME COLUMN {quote(column)} TO {quote(target)}')
        for row in list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")):
            target = renamed(row['name'])
            if target != row['name']:
                sql = re.sub(r'\b' + re.escape(row['name']) + r'\b', target, row['sql'], count=1)
                db.execute('DROP INDEX ' + quote(row['name']))
                db.execute(sql)
        json_columns = {
            'events': ('payload',), 'changes': ('base', 'operations', 'source', 'result'),
            'task_basis': ('point_versions',), 'response_proposals': (),
        }
        for table, columns in json_columns.items():
            if table not in tables(db):
                continue
            for row in list(db.execute(f'SELECT rowid AS _migration_rowid,* FROM {quote(table)}')):
                for column in columns:
                    if column not in row.keys() or row[column] is None:
                        continue
                    try:
                        parsed = json.loads(row[column])
                    except (ValueError, TypeError):
                        continue
                    updated = translate_json(parsed)
                    if updated != parsed:
                        db.execute(f'UPDATE {quote(table)} SET {quote(column)}=? WHERE rowid=?',
                                   (json.dumps(updated, ensure_ascii=False), row['_migration_rowid']))
        if 'events' in tables(db):
            for row in list(db.execute('SELECT id,kind,source_key FROM events')):
                kind = renamed(row['kind']) if row['kind'].startswith(OLD + '.') else row['kind']
                source = row['source_key']
                if source and source.startswith(OLD + '.'):
                    source = renamed(source)
                db.execute('UPDATE events SET kind=?,source_key=? WHERE id=?', (kind, source, row['id']))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def retire_pending(path):
    """Retain old pending proposals as audit, never silently carry consent across protocols."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    count = 0
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS retired_confirmations ('
                   'kind TEXT, identifier TEXT, snapshot TEXT NOT NULL, retired_at REAL NOT NULL, '
                   'reason TEXT NOT NULL, PRIMARY KEY(kind,identifier))')
        selectors = {
            'changes': 'committed_at IS NULL',
            'request_commands': 'confirmation_reference IS NULL',
            'cancellations': 'confirmation_reference IS NULL',
            'response_proposals': 'confirmed=0',
            'drafts': 'id NOT IN (SELECT draft_id FROM requests)',
        }
        for table, where in selectors.items():
            if table not in tables(db) or (table == 'drafts' and 'requests' not in tables(db)):
                continue
            for row in list(db.execute(f'SELECT * FROM {quote(table)} WHERE {where}')):
                db.execute('INSERT OR IGNORE INTO retired_confirmations VALUES(?,?,?,?,?)',
                           (table, row['id'], json.dumps(dict(row), ensure_ascii=False), time.time(),
                            'Schema/API renamed; prepare a fresh native confirmation.'))
                if table == 'changes' and 'change_presentations' in tables(db):
                    db.execute('DELETE FROM change_presentations WHERE change_id=?', (row['id'],))
                db.execute(f'DELETE FROM {quote(table)} WHERE id=?', (row['id'],))
                count += 1
        if 'confirmations' in tables(db):
            db.execute("UPDATE confirmations SET consumed=1,outcome='retired',error='Prepare a fresh confirmation after migration' WHERE consumed=0")
        db.commit()
        return count
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def translate_native_link(body):
    if not isinstance(body, str) or not body.startswith('ANCOR-LINK:'):
        return body
    line, separator, rest = body.partition('\n')
    try:
        metadata = json.loads(line[len('ANCOR-LINK:'):])
    except ValueError:
        return body
    updated = translate_json(metadata)
    return 'ANCOR-LINK:' + json.dumps(updated, sort_keys=True, separators=(',', ':')) + separator + rest if updated != metadata else body


def migrate_native_copy(path):
    """Translate ANCOR bindings in an explicitly supplied offline Kanban COPY only."""
    import hashlib
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    changed = 0
    try:
        db.execute('BEGIN IMMEDIATE')
        if 'tasks' not in tables(db):
            raise ValueError('native source has no tasks table')
        columns = {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}
        for row in list(db.execute('SELECT * FROM tasks')):
            body = translate_native_link(row['body'])
            if body == row['body']:
                continue
            metadata = json.loads(body.split('\n', 1)[0][len('ANCOR-LINK:'):])
            if 'idempotency_key' in columns and str(row['idempotency_key'] or '').startswith('ancor:'):
                token = 'ancor:' + hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                db.execute('UPDATE tasks SET body=?,idempotency_key=? WHERE id=?', (body, token, row['id']))
            else:
                db.execute('UPDATE tasks SET body=? WHERE id=?', (body, row['id']))
            changed += 1
        db.commit()
        return changed
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
