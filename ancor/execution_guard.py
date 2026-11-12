"""Freeze ambiguous Kanban execution outcomes before native auto-redispatch."""
import json
import logging


log = logging.getLogger(__name__)

UNKNOWN_MARKERS = (
    'kanban execution unknown',
    'kanban owner refused',
)
FREEZE_REASON = (
    'Execution result unknown; task frozen for reconciliation. Do not redispatch '
    'until durable results, a live worker, and completed artifacts or side effects '
    'have been checked.'
)


def _native():
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    return kb, connect


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _strings(nested)


def is_execution_unknown(value):
    """Recognize owner/admission ambiguity in a crash payload or error text."""
    lowered = '\n'.join(_strings(value)).casefold()
    return any(marker in lowered for marker in UNKNOWN_MARKERS)


def is_execution_unknown_hold(event):
    """True for the typed block emitted by this guard, not a worker's own block."""
    payload = event.get('payload') or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return False
    return event.get('kind') == 'blocked' and isinstance(payload, dict) and payload.get('reason') == FREEZE_REASON


def freeze_execution_unknown(task_id, *, board=None, run_id=None):
    """Block a reclaimed task when its crash evidence means the result is unknown.

    Native Kanban restores crashed work to its retry lane before lifecycle hooks
    fire. This guard runs on the owner's worker-exit hook and changes that
    immediately retryable state to a typed block. The coordinator then resolves
    the ambiguity instead of launching duplicate side effects.
    """
    kb, connect = _native()
    conn = connect(board=board)
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'crashed' "
            "AND (? IS NULL OR run_id = ?) ORDER BY id DESC LIMIT 1",
            (task_id, run_id, run_id),
        ).fetchone()
        if row is None:
            return False
        payload = row['payload']
        try:
            payload = json.loads(payload) if isinstance(payload, str) else payload
        except (TypeError, ValueError):
            pass
        if not is_execution_unknown(payload):
            return False
        return bool(kb.block_task(conn, task_id, reason=FREEZE_REASON, kind='capability'))
    finally:
        conn.close()


def execution_recovery_hook(board_name):
    """Native ``on_kanban_worker_exited`` observer for one ANCOR board."""
    def on_worker_exited(task_id, *, board=None, run_id=None, outcome=None,
                         exit_kind=None, **_kwargs):
        if not board_name or board != board_name or outcome != 'crashed':
            return None
        if exit_kind == 'rate_limited':
            return None
        try:
            freeze_execution_unknown(task_id, board=board, run_id=run_id)
        except Exception:
            log.exception('ANCOR execution recovery guard failed for task %s', task_id)
        return None

    return on_worker_exited
