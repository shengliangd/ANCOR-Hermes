"""Thin CLI board adapter; no model-mediated operations or native DB writes.

CLI contract inspected in installed hermes_cli/kanban_parser.py and kanban.py.
Call from a thread when used by an async gateway (CLI operations are blocking).
"""
import hashlib
import json
import subprocess


class NativeBoard:
    def __init__(self, board, *, profile='ancor-leader', executable='hermes', run=subprocess.run, env=None):
        self.prefix = [executable, '-p', profile, 'kanban', '--board', board]
        self.run, self.env = run, env

    def _call(self, *args, json_output=True):
        result = self.run(self.prefix + list(args) + (['--json'] if json_output else []), capture_output=True, text=True, check=True, timeout=60, env=self.env)
        return json.loads(result.stdout) if json_output else result.stdout

    @staticmethod
    def events(db_path, task_ids, *, after=0, limit=100):
        """Version-coupled READ ONLY bridge to hermes_cli.kanban_db.list_events.

        Native CLI show omits event IDs, unsuitable for durable deduplication.
        Installed signature: list_events(conn, task_id) -> list[Event]; fields
        id/task_id/kind/payload/created_at/run_id. No init/migration is called.
        Caller persists cursor only AFTER durable ingestion (not leader ack).
        Returned batch bounded; native API still loads each tracked task history.
        """
        import sqlite3
        from dataclasses import asdict
        from pathlib import Path
        from hermes_cli.kanban_db import list_events
        if limit < 1:
            raise ValueError('limit must be positive')
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = {event.id: asdict(event) for task_id in task_ids
                    for event in list_events(conn, task_id) if event.id > after}
        finally:
            conn.close()
        return [rows[key] for key in sorted(rows)[:limit]]

    def list(self):
        return self._call('list', '--archived')

    def show(self, task_id):
        return self._call('show', task_id)['task']

    @staticmethod
    def link(task):
        first = (task.get('body') or '').split('\n', 1)[0]
        prefix = 'ANCOR-LINK:'
        if not first.startswith(prefix):
            return None
        try:
            value = json.loads(first[len(prefix):])
            if set(value) != {'requirement_id', 'revision', 'key'}:
                return None
            if not isinstance(value['requirement_id'], str) or not isinstance(value['key'], str) or type(value['revision']) is not int:
                return None
            return value
        except (ValueError, TypeError):
            return None

    def block(self, task_id, reason):
        self._call('block', task_id, reason, '--kind', 'needs_input', json_output=False)
        task = self.show(task_id)
        if task['status'] not in {'blocked', 'triage'}:
            raise RuntimeError('native block verification failed')
        return task

    def guard(self, revisions):
        """Best-effort stale revision stop, NOT a pre-dispatch atomic fence.

        Missing requirements are considered stale. Native block revokes claims;
        it cannot undo worker side effects or prevent the list/block race. Run on
        confirmed changes and before reconciliation, under coordinator lock.
        """
        self.guard_state = {'stopped': [], 'deferred': [], 'failed': []}
        for task in self.list():
            link = self.link(task)
            if not link or task.get('archived_at') or task['status'] in {'done', 'cancelled', 'blocked'}:
                continue
            if revisions.get(link['requirement_id']) == link['revision']:
                continue
            # Public block only accepts running/ready. Do not rewrite native
            # state to force a transition; retry unsupported cards next scan.
            if task['status'] not in {'running', 'ready'}:
                self.guard_state['deferred'].append(task['id'])
                continue
            try:
                self.block(task['id'], 'ANCOR requirement revision superseded; do not resume')
            except Exception:
                self.guard_state['failed'].append(task['id'])
            else:
                self.guard_state['stopped'].append(task['id'])
        return self.guard_state['stopped']

    def create(self, requirement_id, revision, key, *, title, body, assignee):
        link = {'requirement_id': requirement_id, 'revision': revision, 'key': key}
        encoded = json.dumps(link, sort_keys=True, separators=(',', ':'))
        # Native idempotency + in-body metadata survives a crash before store linking.
        token = 'ancor:' + hashlib.sha256(encoded.encode()).hexdigest()
        task = self._call('create', title, '--body', 'ANCOR-LINK:' + encoded + '\n' + body,
                          '--assignee', assignee, '--idempotency-key', token,
                          '--created-by', 'ancor-leader', '--initial-status', 'running',
                          '--completion-contract', 'local-only')
        actual = self.show(task['id'])
        if self.link(actual) != link:
            raise RuntimeError('native task link verification failed')
        return actual
