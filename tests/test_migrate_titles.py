import json
import sqlite3
from pathlib import Path

import pytest

from ancor.migrate_titles import apply_titles, export_titles
from ancor.store import Store


def legacy_store(path):
    db = sqlite3.connect(path)
    try:
        db.executescript('''
            CREATE TABLE drafts (
                id TEXT PRIMARY KEY, brief TEXT NOT NULL,
                acceptance_criteria TEXT NOT NULL, replacement_of TEXT,
                summary TEXT);
            CREATE TABLE requests (
                id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL,
                brief TEXT NOT NULL, acceptance_criteria TEXT NOT NULL,
                replacement_of TEXT, confirmation_reference TEXT NOT NULL,
                status TEXT NOT NULL);
            CREATE TABLE task_links (
                request_id TEXT NOT NULL, task_id TEXT NOT NULL,
                PRIMARY KEY (request_id, task_id));
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                payload TEXT NOT NULL, source_key TEXT UNIQUE,
                acknowledged INTEGER NOT NULL DEFAULT 0);
        ''')
        db.execute("INSERT INTO drafts VALUES ('d1', 'Legacy brief', 'Legacy criteria', NULL, 'Legacy summary')")
        db.execute("INSERT INTO requests VALUES ('r1', 'd1', 'Legacy brief', 'Legacy criteria', NULL, 'human:1', 'active')")
        db.execute("INSERT INTO task_links VALUES ('r1', 'task-1')")
        db.execute("INSERT INTO events(kind, payload, source_key) VALUES ('request.confirmed', ?, 'confirmed:r1')",
                   (json.dumps({'request_id': 'r1'}),))
        db.commit()
    finally:
        db.close()


def mapping(export):
    export['drafts'][0]['title'] = 'Legacy delivery'
    export['requests'][0]['title'] = 'Legacy delivery'
    return export


def test_export_apply_preserves_rows_and_backs_up_legacy_database(tmp_path):
    database = tmp_path / 'store.db'
    legacy_store(database)
    exported_path = tmp_path / 'titles.json'

    assert export_titles(database, exported_path) == {'drafts': 1, 'requests': 1}
    exported = json.loads(exported_path.read_text())
    assert exported['drafts'][0]['title'] == ''
    assert exported['drafts'][0]['brief'] == 'Legacy brief'
    assert exported['requests'][0]['title'] == ''
    assert exported['requests'][0]['status'] == 'active'

    titles_path = tmp_path / 'filled.json'
    titles_path.write_text(json.dumps(mapping(exported)))
    result = apply_titles(database, titles_path)
    assert result['drafts'] == result['requests'] == 1

    backup = Path(result['backup'])
    assert backup.exists()
    with sqlite3.connect(backup) as old:
        assert 'title' not in {row[1] for row in old.execute('PRAGMA table_info(drafts)')}
        assert old.execute('SELECT brief FROM drafts').fetchone() == ('Legacy brief',)

    store = Store(database)
    assert store.get_draft('d1')['title'] == 'Legacy delivery'
    assert store.get_request('r1')['title'] == 'Legacy delivery'
    assert store.get_request('r1')['status'] == 'active'
    assert store.snapshot()['task_links'] == [
        {'request_id': 'r1', 'task_id': 'task-1'}]
    assert store.pending_events()[0]['source_key'] == 'confirmed:r1'


@pytest.mark.parametrize('mutation, message', [
    (lambda value: value['drafts'][0].update(title=''), 'nonempty'),
    (lambda value: value['drafts'].append(dict(value['drafts'][0])), 'duplicate'),
    (lambda value: value['requests'].append({'id': 'extra', 'title': 'Extra'}), 'exactly'),
    (lambda value: value['requests'][0].update(title='x' * 81), '80'),
])
def test_apply_rejects_incomplete_or_invalid_mapping_before_backup(
        tmp_path, mutation, message):
    database = tmp_path / 'store.db'
    legacy_store(database)
    exported_path = tmp_path / 'titles.json'
    export_titles(database, exported_path)
    value = mapping(json.loads(exported_path.read_text()))
    mutation(value)
    titles_path = tmp_path / 'filled.json'
    titles_path.write_text(json.dumps(value))

    with pytest.raises(ValueError, match=message):
        apply_titles(database, titles_path)
    assert not list(tmp_path.glob('*.pre-title-*'))
    with sqlite3.connect(database) as db:
        assert 'title' not in {row[1] for row in db.execute('PRAGMA table_info(drafts)')}
