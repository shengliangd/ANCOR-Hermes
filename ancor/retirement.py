"""Explicit retirement dispositions in the requirements sidecar, never native SQL.

Call under the coordinator gate. Retirement does not unlink dependencies, archive
workspaces, stop processes, or mark work done. Acceptance must call
``effective_retired`` afresh; recorded intent alone is not a safety assertion.
"""
from .operations import Operations


class Retirement:
    def __init__(self, ctx, store, board=None):
        self.ctx, self.store = ctx, store
        self.operations = Operations(ctx, store, board)
        with store._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS task_retirements (
                task_id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL,
                reason TEXT NOT NULL)''')

    def retire(self, requirement_id, task_id, reason):
        """Persist retirement intent; return its durable three-field mapping."""
        if self.ctx.profile_name != self.ctx.get_config('leader_profile', 'ancor-leader'):
            raise PermissionError('leader profile required')
        self.store._text(reason)
        with self.store._transaction() as db:
            req = self.store._get(db, 'requirements', requirement_id)
            if req['status'] not in {'active', 'invalidated'}:
                raise ValueError('retirement requires active or invalidated requirement')
            if not db.execute('SELECT 1 FROM task_links WHERE requirement_id = ? AND task_id = ?',
                              (requirement_id, task_id)).fetchone():
                raise ValueError('unlinked task')
            self._require_safe(task_id)
            existing = db.execute('SELECT * FROM task_retirements WHERE task_id = ?',
                                  (task_id,)).fetchone()
            if existing:
                if existing['requirement_id'] != requirement_id or existing['reason'] != reason:
                    raise ValueError('task already retired with a different disposition')
                return dict(existing)
            db.execute('INSERT INTO task_retirements VALUES (?, ?, ?)',
                       (task_id, requirement_id, reason))
            return dict(db.execute('SELECT * FROM task_retirements WHERE task_id = ?',
                                   (task_id,)).fetchone())

    def _require_safe(self, task_id):
        detail = self.operations.detail(task_id)
        if detail['task']['status'] not in {'blocked', 'done'}:
            raise ValueError('retirement requires blocked or done task')
        if any(not run.get('ended_at') for run in detail['runs']):
            raise RuntimeError('worker quiescence not established: active run')
        Operations.require_quiescence(detail)

    def effective_retired(self, requirement_id):
        """Return freshly safe task_id -> disposition mappings.

        Revived/non-quiescent tasks are omitted, not erased. Read failures never
        produce an exclusion; other native errors propagate to the caller.
        """
        with self.store._transaction() as db:
            rows = [dict(row) for row in db.execute(
                'SELECT r.* FROM task_retirements r JOIN task_links l '
                'ON l.requirement_id = r.requirement_id AND l.task_id = r.task_id '
                'WHERE r.requirement_id = ? ORDER BY r.task_id', (requirement_id,))]
        effective = {}
        for row in rows:
            try:
                self._require_safe(row['task_id'])
            except (ValueError, RuntimeError):
                continue
            effective[row['task_id']] = row
        return effective
