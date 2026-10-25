"""One-time title migration for ANCOR stores created before title existed.

The migration intentionally does not derive titles. ``export`` writes exact
legacy context with empty title fields; a human or agent fills every title;
``apply`` validates the complete mapping and rebuilds both tables atomically.
"""
import argparse
from contextlib import closing
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .views import TITLE_MAX_CHARS


def _connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _columns(db, table):
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})')}


def _require_legacy(db):
    for table in ('drafts', 'requests'):
        columns = _columns(db, table)
        if not columns:
            raise ValueError(f'missing table: {table}')
        if 'title' in columns:
            raise ValueError(f'{table} already has title; migration is not needed')


def export_titles(db_path, output_path):
    with closing(_connect(db_path)) as db:
        _require_legacy(db)
        drafts = []
        for row in db.execute('SELECT * FROM drafts ORDER BY rowid'):
            item = dict(row)
            drafts.append({
                'id': item['id'],
                'title': '',
                'summary': item.get('summary'),
                'brief': item['brief'],
                'replacement_of': item.get('replacement_of'),
            })
        requests = []
        for row in db.execute('SELECT * FROM requests ORDER BY rowid'):
            item = dict(row)
            requests.append({
                'id': item['id'],
                'title': '',
                'status': item['status'],
                'brief': item['brief'],
                'replacement_of': item.get('replacement_of'),
            })
    Path(output_path).write_text(
        json.dumps({'drafts': drafts, 'requests': requests},
                   ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'drafts': len(drafts), 'requests': len(requests)}


def _validate_title(value):
    if not isinstance(value, str):
        raise ValueError('title must be text')
    value = value.strip()
    if not value:
        raise ValueError('title must be nonempty')
    if '\n' in value or '\r' in value:
        raise ValueError('title must be a single line')
    if len(value) > TITLE_MAX_CHARS:
        raise ValueError(f'title must be at most {TITLE_MAX_CHARS} characters')
    return value


def _mapping(payload, key):
    rows = payload.get(key)
    if not isinstance(rows, list):
        raise ValueError(f'{key} must be a list')
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not row['id'].strip()):
            raise ValueError(f'{key} entries require an id')
        identifier = row['id']
        if identifier in result:
            raise ValueError(f'duplicate id in {key}: {identifier}')
        result[identifier] = _validate_title(row.get('title'))
    return result


def _backup(db_path):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = Path(str(db_path) + '.pre-title-' + stamp)
    source = sqlite3.connect(db_path, timeout=30)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup


def apply_titles(db_path, titles_path):
    payload = json.loads(Path(titles_path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('title mapping must be a JSON object')
    draft_titles = _mapping(payload, 'drafts')
    request_titles = _mapping(payload, 'requests')
    with closing(_connect(db_path)) as db:
        _require_legacy(db)
        drafts = [dict(row) for row in db.execute('SELECT * FROM drafts ORDER BY rowid')]
        requests = [dict(row) for row in db.execute(
            'SELECT * FROM requests ORDER BY rowid')]
        if set(draft_titles) != {row['id'] for row in drafts}:
            raise ValueError('title mapping must cover every draft id exactly')
        if set(request_titles) != {row['id'] for row in requests}:
            raise ValueError('title mapping must cover every request id exactly')

    backup = _backup(db_path)
    db = _connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE drafts_new (
                        id TEXT PRIMARY KEY, title TEXT NOT NULL,
                        brief TEXT NOT NULL,
                        replacement_of TEXT, summary TEXT)''')
        db.execute('''CREATE TABLE requests_new (
                        id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL,
                        title TEXT NOT NULL, brief TEXT NOT NULL,
                        replacement_of TEXT,
                        confirmation_reference TEXT NOT NULL,
                        status TEXT NOT NULL)''')
        for row in drafts:
            db.execute('INSERT INTO drafts_new VALUES (?, ?, ?, ?, ?)', (
                row['id'], draft_titles[row['id']], row['brief'],
                row.get('replacement_of'), row.get('summary')))
        for row in requests:
            db.execute('INSERT INTO requests_new VALUES (?, ?, ?, ?, ?, ?, ?)', (
                row['id'], row['draft_id'], request_titles[row['id']],
                row['brief'], row.get('replacement_of'),
                row['confirmation_reference'], row['status']))
        db.execute('DROP TABLE drafts')
        db.execute('DROP TABLE requests')
        db.execute('ALTER TABLE drafts_new RENAME TO drafts')
        db.execute('ALTER TABLE requests_new RENAME TO requests')
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    return {'drafts': len(drafts), 'requests': len(requests),
            'backup': str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export', help='export legacy records for title assignment')
    export.add_argument('--db', required=True)
    export.add_argument('--out', required=True)
    apply = sub.add_parser('apply', help='apply a complete agent-filled title map')
    apply.add_argument('--db', required=True)
    apply.add_argument('--titles', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'export':
            result = export_titles(args.db, args.out)
        else:
            result = apply_titles(args.db, args.titles)
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f'ANCOR title migration failed: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
