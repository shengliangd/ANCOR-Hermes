import sqlite3
from pathlib import Path

import pytest

from ancor.migrate_requests import migrate_copy
from ancor.store import Store


def test_offline_copy_preserves_legacy_brief_and_sidecars(tmp_path):
    source = tmp_path / 'old.db'
    store = Store(source)
    req = store.confirm(store.create_draft('Keep exact original\n  including whitespace')['id'], 'confirmed')
    with store._transaction() as db:
        db.execute('INSERT INTO request_notes VALUES(?,?,?,?,?,?,NULL)', ('n', req['id'], 'known latest', 3, 1, 2))
    sidecar = Path(str(source) + '.confirmations.db')
    with sqlite3.connect(sidecar) as db:
        db.execute('CREATE TABLE retained(value TEXT)')
        db.execute("INSERT INTO retained VALUES('original')")
    drafts = Path(str(source) + '.drafts')
    drafts.mkdir()
    (drafts / 'confirmation.txt').write_text('Exact old presentation')
    before = source.read_bytes()
    output = tmp_path / 'new.db'
    result = migrate_copy(source, output)
    assert source.read_bytes() == before
    assert result['legacy_requests'] == 1
    assert result['notes_with_unrecoverable_history'] == 1
    migrated = Store(output)
    assert migrated.content(req['id'])['legacy_brief'] == req['brief']
    assert migrated.snapshot()['task_links'] == store.snapshot()['task_links']
    assert Path(str(output) + '.confirmations.db').exists()
    assert (Path(str(output) + '.drafts') / 'confirmation.txt').read_text() == 'Exact old presentation'
    with pytest.raises(ValueError, match='must not exist'):
        migrate_copy(source, output)


def old_database(path):
    import json
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE drafts(id TEXT PRIMARY KEY,title TEXT NOT NULL,brief TEXT NOT NULL,replacement_of TEXT,summary TEXT);
            CREATE TABLE requirements(id TEXT PRIMARY KEY,draft_id TEXT NOT NULL UNIQUE,title TEXT NOT NULL,brief TEXT NOT NULL,
                replacement_of TEXT,confirmation_reference TEXT NOT NULL,status TEXT NOT NULL,target_status TEXT);
            CREATE TABLE task_links(requirement_id TEXT NOT NULL,task_id TEXT NOT NULL,PRIMARY KEY(requirement_id,task_id));
            CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,payload TEXT NOT NULL,source_key TEXT UNIQUE,acknowledged INTEGER NOT NULL DEFAULT 0);
        ''')
        db.execute("INSERT INTO drafts VALUES('d','Original title','User said requirement, keep it verbatim',NULL,NULL)")
        db.execute("INSERT INTO requirements VALUES('r','d','Original title','User said requirement, keep it verbatim',NULL,'confirmed','active',NULL)")
        db.execute("INSERT INTO task_links VALUES('r','t')")
        db.execute('INSERT INTO events(kind,payload,source_key) VALUES(?,?,?)',
                   ('requirement.activated', json.dumps({'requirement_id': 'r', 'body': 'Keep requirement in human text'}), 'requirement.create:r'))
        db.execute("INSERT INTO drafts VALUES('pending','Pending','Never confirmed',NULL,NULL)")


def test_old_vocabulary_is_offline_only_and_source_is_unchanged(tmp_path):
    import json
    source, output = tmp_path / 'old.db', tmp_path / 'new.db'
    old_database(source)
    original = source.read_bytes()
    with pytest.raises(RuntimeError, match='migrate_requests'):
        Store(source)
    assert source.read_bytes() == original
    result = migrate_copy(source, output)
    assert source.read_bytes() == original
    assert result['retired_pending_proposals'] == 1
    store = Store(output)
    assert store.get_request('r')['brief'] == 'User said requirement, keep it verbatim'
    assert store.snapshot()['task_links'] == [{'request_id': 'r', 'task_id': 't'}]
    event = store.pending_events()[0]
    assert event['kind'] == 'request.activated' and event['payload']['request_id'] == 'r'
    assert event['payload']['body'] == 'Keep requirement in human text'
    with sqlite3.connect(output) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='requirements'").fetchone()
        assert db.execute("SELECT COUNT(*) FROM retired_confirmations").fetchone()[0] == 1
    with pytest.raises(KeyError):
        store.get_draft('pending')


def test_native_ancor_metadata_and_idempotency_are_migrated_in_a_copy(tmp_path):
    import hashlib
    import json
    source, output, native = tmp_path / 'old.db', tmp_path / 'new.db', tmp_path / 'native.db'
    old_database(source)
    metadata = {'requirement_id': 'r', 'revision': 1, 'key': 'k'}
    with sqlite3.connect(native) as db:
        db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,body TEXT,idempotency_key TEXT)')
        db.execute('INSERT INTO tasks VALUES(?,?,?)', ('t','ANCOR-LINK:' + json.dumps(metadata) + '\nOriginal contract', 'ancor:old'))
        db.execute("INSERT INTO tasks VALUES('other','Unmanaged task',NULL)")
    before = native.read_bytes()
    result = migrate_copy(source, output, native_source=native)
    assert result['translated_native_links'] == 1 and native.read_bytes() == before
    with sqlite3.connect(str(output) + '.kanban.db') as db:
        body, token = db.execute("SELECT body,idempotency_key FROM tasks WHERE id='t'").fetchone()
        link = json.loads(body.split('\n')[0].split(':', 1)[1])
        assert link == {'request_id': 'r', 'revision': 1, 'key': 'k'}
        assert body.endswith('\nOriginal contract')
        assert token == 'ancor:' + hashlib.sha256(json.dumps(link, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert db.execute("SELECT body FROM tasks WHERE id='other'").fetchone()[0] == 'Unmanaged task'
