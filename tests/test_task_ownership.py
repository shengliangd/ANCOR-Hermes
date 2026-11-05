import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ancor.store import Store


def test_task_owner_is_unique_under_concurrent_association(tmp_path):
    path = tmp_path / 'requests.db'
    store = Store(path)
    requests = [store.confirm(store.create_draft('work', title=title)['id'], 'test') for title in ['First', 'Second']]
    def associate(req):
        try:
            Store(path).associate_task('task', req['id'], 'claim')
            return req['id']
        except ValueError as exc:
            assert 'task ownership conflict' in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [rid for rid in pool.map(associate, requests) if rid]
    assert len(winners) == 1
    store.link_task(winners[0], 'task')
    store.associate_task('task', winners[0], 'idempotent')
    loser = next(req['id'] for req in requests if req['id'] != winners[0])
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute('INSERT INTO task_links VALUES (?, ?)', (loser, 'task'))
    assert len(Store(path).snapshot()['task_links']) == 1


def test_existing_duplicate_owners_require_explicit_repair(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE task_links (request_id TEXT, task_id TEXT, PRIMARY KEY(request_id, task_id))')
        db.executemany('INSERT INTO task_links VALUES (?, ?)', [('first', 'task'), ('second', 'task')])
    with pytest.raises(ValueError, match='task ownership conflict'):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM task_links').fetchone()[0] == 2
