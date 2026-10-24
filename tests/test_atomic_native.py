"""Native lifecycle probe on a disposable database; no dispatcher/model or live board."""
from dataclasses import asdict
import os
from pathlib import Path

import pytest

from ancor.transitions import require_quiescence


def test_native_block_edit_resume_preserves_other_work(tmp_path, monkeypatch):
    source = Path(os.environ.get('ANCOR_HERMES_SOURCE', '/opt/ancor-native/.hermes/hermes-agent'))
    if not source.is_dir():
        pytest.skip('set ANCOR_HERMES_SOURCE to installed Hermes source')
    monkeypatch.syspath_prepend(str(source))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect
    except ImportError as exc:
        pytest.skip(f'native dependencies unavailable: {exc}')
    conn = connect(tmp_path / 'native.db')
    try:
        affected = kb.create_task(conn, title='Affected', body='old contract', initial_status='blocked',
                                  completion_contract='local-only', workspace_kind='scratch')
        unrelated = kb.create_task(conn, title='Unrelated', body='independent', initial_status='blocked',
                                   completion_contract='local-only', workspace_kind='scratch')
        assert kb.unblock_task(conn, affected)
        assert kb.unblock_task(conn, unrelated)
        assert kb.claim_task(conn, affected)
        assert kb.claim_task(conn, unrelated)
        before = asdict(kb.get_task(conn, unrelated))
        assert kb.block_task(conn, affected, reason='Requirement revision', kind='needs_input')
        require_quiescence({'runs': [asdict(r) for r in kb.list_runs(conn, affected)]})
        assert kb.edit_task(conn, affected, body='new contract')
        assert kb.unblock_task(conn, affected)
        assert kb.get_task(conn, affected).body == 'new contract'
        assert kb.get_task(conn, affected).status == 'ready'
        assert asdict(kb.get_task(conn, unrelated)) == before
    finally:
        conn.close()
