import sqlite3
from pathlib import Path

import pytest

from ancor.migrate_points import migrate_copy
from ancor.store import Store


def test_offline_copy_preserves_legacy_brief_and_sidecars(tmp_path):
    source = tmp_path / 'old.db'
    store = Store(source)
    req = store.confirm(store.create_draft('Keep exact original\n  including whitespace')['id'], 'confirmed')
    with store._transaction() as db:
        db.execute('INSERT INTO requirement_notes VALUES(?,?,?,?,?,?,NULL)', ('n', req['id'], 'known latest', 3, 1, 2))
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
    assert result['legacy_requirements'] == 1
    assert result['notes_with_unrecoverable_history'] == 1
    migrated = Store(output)
    assert migrated.content(req['id'])['legacy_brief'] == req['brief']
    assert migrated.snapshot()['task_links'] == store.snapshot()['task_links']
    assert Path(str(output) + '.confirmations.db').exists()
    assert (Path(str(output) + '.drafts') / 'confirmation.txt').read_text() == 'Exact old presentation'
    with pytest.raises(ValueError, match='must not exist'):
        migrate_copy(source, output)
