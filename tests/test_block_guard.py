"""Native unblock-loop guard: keep ANCOR cards out of native triage."""
import contextlib
from types import SimpleNamespace

import ancor
import ancor.block_guard as guard


class Conn:
    def __init__(self, kinds=()):
        self.kinds = list(kinds)
        self.executed = []
        self.closed = False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def close(self):
        self.closed = True


def native(conn):
    events = [SimpleNamespace(kind=kind) for kind in conn.kinds]
    kb = SimpleNamespace(list_events=lambda _conn, _task: events,
                         write_txn=lambda _conn: contextlib.nullcontext())
    return kb, lambda board=None: conn


def test_reset_below_the_ceiling(monkeypatch):
    conn = Conn(kinds=['blocked', 'unblocked', 'blocked', 'unblocked'])
    monkeypatch.setattr(guard, '_native', lambda: native(conn))
    assert guard.reset_block_recurrence('t1', board='ancor', ceiling=3) == 'reset'
    assert 'block_recurrences = 0' in conn.executed[0][0]
    assert conn.executed[0][1] == ('t1',)
    assert conn.closed


def test_ceiling_keeps_native_triage_reachable(monkeypatch):
    conn = Conn(kinds=['unblocked'] * 3)
    monkeypatch.setattr(guard, '_native', lambda: native(conn))
    assert guard.reset_block_recurrence('t1', board='ancor', ceiling=3) == 'ceiling'
    assert not conn.executed
    assert conn.closed


def test_zero_ceiling_disables_the_ceiling(monkeypatch):
    conn = Conn(kinds=['unblocked'] * 9)
    monkeypatch.setattr(guard, '_native', lambda: native(conn))
    assert guard.reset_block_recurrence('t1', board='ancor', ceiling=0) == 'reset'


def test_hook_only_touches_the_ancor_board(monkeypatch):
    seen = []
    monkeypatch.setattr(guard, 'reset_block_recurrence',
                        lambda *args, **kwargs: seen.append((args, kwargs)))
    hook = guard.block_guard_hook('ancor')
    assert hook('t1', board='other') is None
    assert not seen
    assert hook('t1', board='ancor') is None
    assert seen == [(('t1',), {'board': 'ancor',
                              'ceiling': guard.DEFAULT_UNBLOCK_CEILING})]


def test_hook_is_inert_without_a_configured_board(monkeypatch):
    seen = []
    monkeypatch.setattr(guard, 'reset_block_recurrence',
                        lambda *args, **kwargs: seen.append(args))
    hook = guard.block_guard_hook(None)
    assert hook('t1', board='ancor') is None
    assert not seen


def test_hook_swallows_guard_failures(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('db down')

    monkeypatch.setattr(guard, 'reset_block_recurrence', boom)
    hook = guard.block_guard_hook('ancor')
    assert hook('t1', board='ancor') is None


def test_hook_coerces_the_configured_ceiling(monkeypatch):
    seen = []
    monkeypatch.setattr(guard, 'reset_block_recurrence',
                        lambda *args, **kwargs: seen.append(kwargs['ceiling']))
    guard.block_guard_hook('ancor', '5')('t1', board='ancor')
    guard.block_guard_hook('ancor', 'not a number')('t1', board='ancor')
    assert seen == [5, guard.DEFAULT_UNBLOCK_CEILING]


def registered_hooks(profile, **overrides):
    hooks = {}
    config = {'board': 'ancor', **overrides}
    ctx = SimpleNamespace(profile_name=profile,
                          get_config=lambda key, default=None: config.get(key, default),
                          register_hook=lambda name, callback: hooks.setdefault(name, []).append(callback),
                          register_tool=lambda **kwargs: None,
                          register_platform_handler=lambda *_args: None)
    ancor.register(ctx)
    return hooks


def test_coordinator_and_worker_register_the_block_observer():
    for profile in ('ancor-coordinator', 'ancor-worker'):
        assert 'kanban_task_blocked' in registered_hooks(profile), profile


def test_interactor_does_not_register_the_block_observer():
    assert 'kanban_task_blocked' not in registered_hooks('ancor-interactor')


def test_gateway_owner_registers_execution_recovery_and_block_guard():
    hooks = registered_hooks('default', gateway_owner=True)
    assert 'on_kanban_worker_exited' in hooks
    assert 'kanban_task_blocked' in hooks
