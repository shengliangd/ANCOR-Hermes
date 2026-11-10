"""Native capability probe: authorized parent runs with ANCOR_NATIVE_TEST=1.

Never remove delegation markers to run this test. Uses only native domain APIs
on tmp_path; no dispatcher/model and no live board mutations.
"""
import os
from dataclasses import asdict

import pytest

from ancor.board import NativeBoard
from native_hermes import native_source_or_skip


@pytest.mark.skipif(os.environ.get('ANCOR_NATIVE_TEST') != '1', reason='authorized parent native probe only')
@pytest.mark.parametrize('gated', [False, True])
def test_native_revise_preserves_scope_and_parent_gating(tmp_path, monkeypatch, gated):
    monkeypatch.syspath_prepend(str(native_source_or_skip()))
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    path = tmp_path / 'kanban.db'
    conn = connect(path)
    try:
        parents = []
        if gated:
            parents.append(kb.create_task(conn, title='Prerequisite', initial_status='blocked'))
        task = kb.create_task(conn, title='Reviewed scope', body='Exact body\n  retained\n',
                              triage=True, parents=parents, assignee='ancor-worker')
        before = kb.get_task(conn, task)
        board = NativeBoard('isolated', env={'HERMES_KANBAN_DB': str(path)})
        monkeypatch.setattr(board, '_call', lambda *a, **k: {'task': asdict(kb.get_task(conn, task))})
        result = board.revise(task, 'Coordinator reviewed scope; release approved')
        assert result['task']['status'] == ('todo' if gated else 'ready')
        after = kb.get_task(conn, task)
        assert (after.title, after.body, after.assignee) == (before.title, before.body, before.assignee)
        assert any(c.body == 'Coordinator reviewed scope; release approved' for c in kb.list_comments(conn, task))
        assert any(e.kind == 'specified' for e in kb.list_events(conn, task))
    finally:
        conn.close()


def test_create_planning_argv_and_readback():
    calls = []
    board = NativeBoard('isolated')
    task = {}
    def call(*args, **kwargs):
        calls.append(args)
        if args[0] == 'create':
            task.update(id='child', status='blocked', body=args[args.index('--body') + 1])
            return task
        return {'task': task, 'parents': ['p1', 'p2']}
    board._call = call
    board.create('r1', 1, 'k', title='Child', body='Work', assignee='ancor-worker', parent_ids=['p1', 'p2'], hold=True)
    argv = calls[0]
    assert argv[argv.index('--initial-status') + 1] == 'blocked'
    assert [argv[i + 1] for i, value in enumerate(argv) if value == '--parent'] == ['p1', 'p2']
    assert not any(c[0] == 'link' for c in calls)
