"""Thin CLI adapter, read-only native metadata/event bridges, and version-coupled triage revision.

No raw SQL in this adapter: the one native write ANCOR owns is the block-recurrence
counter reset in :mod:`ancor.block_guard`, reached through ``keep_out_of_native_triage``.

CLI contract inspected in installed hermes_cli/kanban_parser.py and kanban.py.
Call from a thread when used by an async gateway (CLI operations are blocking).
"""
import hashlib
import json
import logging
import subprocess

log = logging.getLogger(__name__)


class NativeBoard:
    def __init__(self, board, *, profile='ancor-coordinator', executable='hermes', run=subprocess.run, env=None):
        self.board = board
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
        Caller persists cursor only AFTER durable ingestion (not coordinator ack).
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
        tasks = self._call('list', '--archived')
        try:
            metadata = self.metadata()
        except Exception:
            return tasks
        for task in tasks:
            detail = metadata.get(task['id'])
            if detail is not None:
                task.update(last_heartbeat_at=detail.last_heartbeat_at,
                            block_kind=detail.block_kind,
                            block_recurrences=detail.block_recurrences)
        return tasks

    def metadata(self):
        """Read fields omitted by native list JSON through its read-only API."""
        import sqlite3
        from pathlib import Path
        from hermes_cli.kanban_db import kanban_db_path, list_tasks
        path = kanban_db_path(board=self.board)
        conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return {task.id: task for task in list_tasks(conn, include_archived=True)}
        finally:
            conn.close()

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
            if set(value) != {'request_id', 'revision', 'key'}:
                return None
            if not isinstance(value['request_id'], str) or not isinstance(value['key'], str) or type(value['revision']) is not int:
                return None
            return value
        except (ValueError, TypeError):
            return None

    def edit(self, task_id, *, title=None, body=None, priority=None):
        fields = {key: value for key, value in
                  (('title', title), ('body', body), ('priority', priority)) if value is not None}
        if not fields:
            raise ValueError('provide title, body or priority')
        self._call('edit', task_id, *(f'--{key}={value}' for key, value in fields.items()), json_output=False)
        actual = self._call('show', task_id)
        if any(actual['task'][key] != value for key, value in fields.items()):
            raise RuntimeError('native edit verification failed')
        return actual

    def dependency(self, action, parent_id, child_id):
        """Native prerequisite edge, not an ANCOR request association."""
        if action not in {'link', 'unlink'}:
            raise ValueError('unknown dependency action')
        self._call(action, parent_id, child_id, json_output=False)
        actual = self._call('show', child_id)
        if (parent_id in actual['parents']) != (action == 'link'):
            raise RuntimeError('native dependency verification failed')
        return actual

    def revise(self, task_id, reason):
        """Version-coupled native domain adapter, not CLI specify (auxiliary LLM).

        Requires kanban_db_connect.connect and kanban_db domain functions.
        Native connect/write_txn retain delegated-child fences; never clear
        lineage or use raw SQL. Coordinator edits scope before this explicit review.
        Omitted fields preserve exact existing title/body/assignee. Audit intent
        is committed BEFORE release; native specify cannot nest in an outer txn.
        A refused release can therefore leave its truthful review-intent comment.
        """
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect
        from pathlib import Path
        reason = reason.strip()
        if not reason:
            raise ValueError('reason is required')
        path = (self.env or {}).get('HERMES_KANBAN_DB')
        conn = connect(db_path=Path(path) if path else None, board=self.prefix[5])
        try:
            before = kb.get_task(conn, task_id)
            if before is None or before.status != 'triage':
                raise ValueError('revise requires an unarchived triage task')
            comment_id = kb.add_comment(conn, task_id, self.prefix[2], reason)
            if not kb.specify_triage_task(conn, task_id, author=self.prefix[2]):
                raise RuntimeError('native revise refused; review intent recorded')
            after = kb.get_task(conn, task_id)
            comments = kb.list_comments(conn, task_id)
            if (after is None or after.status not in {'todo', 'ready', 'running'}
                    or any(getattr(after, key) != getattr(before, key)
                           for key in ('title', 'body', 'assignee'))
                    or not any(c.id == comment_id and c.body == reason
                               and c.author == self.prefix[2] for c in comments)):
                raise RuntimeError('native revise verification failed')
        finally:
            conn.close()
        return self._call('show', task_id)

    def block(self, task_id, reason):
        self._call('block', task_id, reason, '--kind', 'needs_input', json_output=False)
        task = self.show(task_id)
        if task['status'] not in {'blocked', 'triage'}:
            raise RuntimeError('native block verification failed')
        self.keep_out_of_native_triage(task_id)
        return task

    def keep_out_of_native_triage(self, task_id):
        """Clear the native unblock-loop counter; ANCOR owns loop control here.

        The native breaker routes the second same-kind block to ``triage``. ANCOR
        repairs a card by editing it, so unrelated causes would be counted as a
        loop. Zeroing after every block keeps ``block_loop_detected`` unwritten;
        ``block_guard`` stops resetting once a card uses up its repair cycles.
        Failing to reset must never fail the block itself.
        """
        from .block_guard import reset_block_recurrence
        try:
            reset_block_recurrence(task_id, board=self.board)
        except Exception:
            log.exception('ANCOR block guard failed for task %s', task_id)

    def archive(self, task_id, reason):
        """Write an audit comment, then use native archive and verify it stuck."""
        reason = reason.strip()
        if not reason:
            raise ValueError('archive reason is required')
        task = self.show(task_id)
        if task['status'] != 'archived':
            self._call('comment', task_id, reason, '--author', self.prefix[2],
                       json_output=False)
            self._call('archive', task_id, json_output=False)
        actual = self.show(task_id)
        if actual['status'] != 'archived':
            raise RuntimeError('native archive verification failed')
        return actual

    def guard(self, revisions):
        """Best-effort stale revision stop, NOT a pre-dispatch atomic fence.

        Missing requests are considered stale. Native block revokes claims;
        it cannot undo worker side effects or prevent the list/block race. Run on
        confirmed changes and before reconciliation, under coordinator lock.
        """
        self.guard_state = {'stopped': [], 'deferred': [], 'failed': []}
        for task in self.list():
            link = self.link(task)
            if not link or task.get('archived_at') or task['status'] in {'done', 'cancelled', 'blocked'}:
                continue
            if revisions.get(link['request_id']) == link['revision']:
                continue
            # Public block only accepts running/ready. Do not rewrite native
            # state to force a transition; retry unsupported cards next scan.
            if task['status'] not in {'running', 'ready'}:
                self.guard_state['deferred'].append(task['id'])
                continue
            try:
                self.block(task['id'], 'ANCOR request revision superseded; do not resume')
            except Exception:
                self.guard_state['failed'].append(task['id'])
            else:
                self.guard_state['stopped'].append(task['id'])
        return self.guard_state['stopped']

    def create(self, request_id, revision, key, *, title, body, assignee, parent_ids=(), hold=False):
        link = {'request_id': request_id, 'revision': revision, 'key': key}
        encoded = json.dumps(link, sort_keys=True, separators=(',', ':'))
        # Native idempotency + in-body metadata survives a crash before store linking.
        token = 'ancor:' + hashlib.sha256(encoded.encode()).hexdigest()
        task = self._call('create', title, '--body', 'ANCOR-LINK:' + encoded + '\n' + body,
                          '--assignee', assignee, '--idempotency-key', token,
                          '--created-by', 'ancor-coordinator', '--initial-status', 'blocked' if hold else 'running',
                          *(flag for parent in parent_ids for flag in ('--parent', parent)),
                          '--completion-contract', 'local-only')
        detail = self._call('show', task['id'])
        actual = detail['task']
        if set(parent_ids) - set(detail.get('parents', [])) or (hold and actual['status'] != 'blocked'):
            raise RuntimeError('native creation planning verification failed')
        if self.link(actual) != link:
            raise RuntimeError('native task link verification failed')
        return actual

    def transfer(self, task_id, request_id, revision, key, body):
        encoded = json.dumps({'request_id': request_id, 'revision': revision, 'key': key}, sort_keys=True, separators=(',', ':'))
        return self.edit(task_id, body='ANCOR-LINK:' + encoded + '\n' + body)
