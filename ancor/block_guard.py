"""Keep ANCOR's repair cycle out of the native unblock-loop breaker.

Native Kanban routes the second same-kind block on one card to ``triage``
(``BLOCK_RECURRENCE_LIMIT``). ANCOR repairs a blocked card by editing it and
unblocking it, so that counter fires on unrelated causes. The coordinator owns
loop control here, so every block zeroes the native recurrence counter: the
counter cannot reach the limit and ``block_loop_detected`` is never written.

A per-card ceiling keeps a real backstop. Past ``DEFAULT_UNBLOCK_CEILING``
resumes the counter is left alone, so a card that keeps failing still routes to
``triage``.
"""
import logging

log = logging.getLogger(__name__)

DEFAULT_UNBLOCK_CEILING = 3


def should_reset(unblocked_cycles, ceiling=DEFAULT_UNBLOCK_CEILING):
    """True while this card has not yet used up its repair cycles."""
    return ceiling <= 0 or unblocked_cycles < ceiling


def _native():
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    return kb, connect


def reset_block_recurrence(task_id, *, board=None, ceiling=DEFAULT_UNBLOCK_CEILING):
    """Zero the native block-recurrence counter for one card.

    Returns ``'reset'`` when the counter was cleared, ``'ceiling'`` when the card
    has already used its repair cycles and native triage should stay reachable.
    """
    kb, connect = _native()
    conn = connect(board=board)
    try:
        cycles = sum(1 for event in kb.list_events(conn, task_id)
                     if event.kind == 'unblocked')
        if not should_reset(cycles, ceiling):
            return 'ceiling'
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET block_recurrences = 0 WHERE id = ?',
                         (task_id,))
        return 'reset'
    finally:
        conn.close()


def block_guard_hook(board_name, ceiling=DEFAULT_UNBLOCK_CEILING):
    """Native ``kanban_task_blocked`` observer for one ANCOR board.

    Nothing here may break the worker's block: a guard failure is logged and the
    native block stands as recorded.
    """
    try:
        ceiling = int(ceiling)
    except (TypeError, ValueError):
        ceiling = DEFAULT_UNBLOCK_CEILING

    def on_task_blocked(task_id, board=None, **_kwargs):
        if not board_name or board != board_name:
            return None
        try:
            reset_block_recurrence(task_id, board=board, ceiling=ceiling)
        except Exception:
            log.exception('ANCOR block guard failed for task %s', task_id)
        return None

    return on_task_blocked
