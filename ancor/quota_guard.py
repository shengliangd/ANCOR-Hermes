"""Generic tool-wrapper resource quota guard enforced via GOAL_ID env var.

Supports arbitrary numeric resource keys (budget.limits, e.g. gpu.alaya,
cpu_cores, api_usd, disk_gb) and discrete allowlists (budget.allowed, e.g.
clusters, writable_paths, accounts). Resources not constrained by limits or
expressed only in natural-language budget.constraints are not blocked.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from uuid import uuid4

from .goals import GoalStore, QuotaExceededError


class QuotaGuard:
    """Wrapper-level generic resource quota enforcement."""

    def __init__(self, goal_store: GoalStore, cluster_backend=None):
        self.goal_store = goal_store
        self.cluster_backend = cluster_backend

    def acquire(self, *, usage: dict[str, float | int] | None = None,
                tags: dict[str, str] | None = None,
                command: str | list[str] | None = None,
                handle_id: str | None = None,
                goal_id: str | None = None,
                metadata: dict | None = None) -> dict:
        resolved_goal = (goal_id or os.environ.get('GOAL_ID') or '').strip()
        if not resolved_goal:
            raise PermissionError('GOAL_ID environment variable is required for resource acquisition')
        hid = (handle_id or f'res_{uuid4().hex[:10]}').strip()
        usage = dict(usage or {})
        tags = dict(tags or {})
        meta = dict(metadata or {})
        if command is not None:
            meta['command'] = command

        # Fail closed: validate and atomically reserve quota BEFORE touching backend.
        entry = self.goal_store.register_resource_usage(
            resolved_goal, handle_id=hid, usage=usage, tags=tags, metadata=meta)

        if self.cluster_backend is not None:
            try:
                backend_info = self.cluster_backend.submit_handle(
                    handle_id=hid, usage=usage, tags=tags,
                    goal_id=resolved_goal, command=command, metadata=meta)
                if isinstance(backend_info, dict):
                    entry = {**entry, 'backend': backend_info}
            except Exception as exc:
                self._mark_failed(resolved_goal, hid, str(exc))
                raise
        return entry

    def _mark_failed(self, goal_id: str, handle_id: str, reason: str) -> None:
        with self.goal_store.store._transaction() as db:
            goal = self.goal_store._get(db, goal_id)
            updated = [
                ({**h, 'status': 'failed', 'detail': reason} if h['handle_id'] == handle_id else h)
                for h in goal['external_job_ids']
            ]
            db.execute(
                'UPDATE active_goals SET external_job_ids=?, updated_at=? WHERE goal_id=?',
                (json.dumps(updated, ensure_ascii=False), float(self.goal_store.clock()), goal_id))


def _parse_kv(items: list[str], *, numeric: bool) -> dict:
    out = {}
    for raw in items:
        if '=' not in raw:
            raise ValueError(f'expected key=value, got {raw!r}')
        key, val = raw.split('=', 1)
        key, val = key.strip(), val.strip()
        if not key or not val:
            raise ValueError(f'expected non-empty key=value, got {raw!r}')
        out[key] = float(val) if (numeric and '.' in val) else int(val) if numeric else val
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='ANCOR generic resource quota wrapper')
    parser.add_argument('--store', required=True, help='Path to requirements.db')
    parser.add_argument('--use', action='append', default=[],
                        help='Resource usage key=amount (repeatable, e.g. --use gpu.alaya=2 --use api_usd=5)')
    parser.add_argument('--tag', action='append', default=[],
                        help='Allowlist tag key=value (repeatable, e.g. --tag clusters=alaya)')
    parser.add_argument('--handle-id', help='Optional explicit handle/job ID')
    parser.add_argument('--goal-id', help='Optional explicit GOAL_ID (defaults to env GOAL_ID)')
    parser.add_argument('cmd', nargs=argparse.REMAINDER, help='Optional command to execute after --')
    args = parser.parse_args(argv)

    from .store import Store
    store = Store(args.store)
    guard = QuotaGuard(GoalStore(store))
    cmd = list(args.cmd)
    if cmd and cmd[0] == '--':
        cmd = cmd[1:]
    try:
        usage = _parse_kv(args.use, numeric=True)
        tags = _parse_kv(args.tag, numeric=False)
        entry = guard.acquire(
            usage=usage, tags=tags, handle_id=args.handle_id,
            goal_id=args.goal_id, command=cmd or None)
    except (QuotaExceededError, PermissionError, ValueError, KeyError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    if cmd:
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            guard._mark_failed(args.goal_id or os.environ['GOAL_ID'], entry['handle_id'], f'rc={proc.returncode}')
        return proc.returncode
    print(json.dumps({'ok': True, 'handle': entry}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
