import json
from types import SimpleNamespace

import ancor.execution_guard as guard


class Conn:
    def __init__(self, payload):
        self.payload = payload
        self.query = None
        self.closed = False

    def execute(self, sql, params):
        self.query = (sql, params)
        return SimpleNamespace(fetchone=lambda: {'payload': json.dumps(self.payload)})

    def close(self):
        self.closed = True


def native(conn):
    calls = []
    def block_task(_conn, task_id, **kwargs):
        calls.append((task_id, kwargs))
        return True
    kb = SimpleNamespace(block_task=block_task)
    return kb, lambda board=None: conn, calls


def test_execution_unknown_detection_handles_nested_owner_errors():
    assert guard.is_execution_unknown({
        'worker_output': 'Session ended\nKanban owner refused: Kanban execution unknown'})
    assert not guard.is_execution_unknown({
        'worker_output': 'ordinary model failure'})


def test_freeze_blocks_an_unknown_crash_with_recovery_reason(monkeypatch):
    conn = Conn({'worker_output': 'Kanban owner refused: Kanban execution unknown'})
    kb, connect, calls = native(conn)
    monkeypatch.setattr(guard, '_native', lambda: (kb, connect))

    assert guard.freeze_execution_unknown('t1', board='ancor', run_id=42)
    assert calls == [('t1', {'reason': guard.FREEZE_REASON, 'kind': 'capability'})]
    assert 'run_id = ?' in conn.query[0]
    assert conn.query[1] == ('t1', 42, 42)
    assert conn.closed


def test_freeze_ignores_ordinary_crashes(monkeypatch):
    conn = Conn({'worker_output': 'ordinary model failure'})
    kb, connect, calls = native(conn)
    monkeypatch.setattr(guard, '_native', lambda: (kb, connect))

    assert not guard.freeze_execution_unknown('t1', board='ancor', run_id=42)
    assert calls == []
    assert conn.closed


def test_owner_hook_only_freezes_unknown_crashes_on_the_configured_board(monkeypatch):
    seen = []
    monkeypatch.setattr(guard, 'freeze_execution_unknown',
                        lambda *args, **kwargs: seen.append((args, kwargs)))
    hook = guard.execution_recovery_hook('ancor')

    assert hook('t1', board='other', run_id=1, outcome='crashed') is None
    assert hook('t1', board='ancor', run_id=2, outcome='completed') is None
    assert hook('t1', board='ancor', run_id=3, outcome='crashed',
                exit_kind='rate_limited') is None
    assert hook('t1', board='ancor', run_id=4, outcome='crashed',
                exit_kind='nonzero_exit') is None
    assert seen == [(('t1',), {'board': 'ancor', 'run_id': 4})]


def test_owner_hook_swallows_recovery_failures(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(guard, 'freeze_execution_unknown', boom)
    hook = guard.execution_recovery_hook('ancor')
    assert hook('t1', board='ancor', run_id=4, outcome='crashed') is None


def test_execution_unknown_hold_is_not_a_new_worker_block():
    hold = {'kind': 'blocked', 'payload': {'reason': guard.FREEZE_REASON}}
    assert guard.is_execution_unknown_hold(hold)
    assert not guard.is_execution_unknown_hold({
        'kind': 'blocked', 'payload': {'reason': 'missing input'}})
