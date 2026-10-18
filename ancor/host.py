"""Gateway-owned polling bridge; no model calls without durable pending work.

Native boundary: adapter.is_connected, NativeBoard CLI, and read-only native
list_events/kanban_db_path. No native SQL or guard overrides. Retry budget is
persisted beside the store: three attempts per identical batch, then a one-hour
cooldown (not an acknowledgement). New batches can run immediately.
"""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import time

from .board import NativeBoard
from .runtime import Coordinator
from .store import Store

log = logging.getLogger(__name__)


def child_environment(environ=None):
    """Drop routing identity only; retain ALL native task/delegation fences."""
    source = dict(os.environ if environ is None else environ)
    return {k: v for k, v in source.items()
            if not k.startswith('HERMES_SESSION_') and k not in {
                'HERMES_UI_SESSION_ID', 'HERMES_BROWSER_CONTROL_PRINCIPAL',
                'HERMES_BROWSER_CONTROL_TRANSPORT_FAMILY', 'HERMES_CRON_SESSION'}}


def leader_command(profile, prompt):
    # env -u affects only the child, never concurrent Gateway sessions. Explicit
    # -p is resolved by the native CLI before importing profile-scoped modules.
    clean = child_environment()
    removed = sorted(set(os.environ) - set(clean))
    return ['env', *[part for key in removed for part in ('-u', key)],
            'ANCOR_COORDINATOR=1', 'hermes', '-p', profile, '--cli', 'chat', '-q', prompt]


class Host:
    def __init__(self, ctx, *, board=None, store=None, db_path=None, clock=time.time):
        path = Path(ctx.get_config('store_path', ''))
        if not path.is_absolute():
            raise ValueError('configure shared absolute store_path for ANCOR')
        self.profile = ctx.get_config('leader_profile', 'ancor-leader')
        self.board_name = ctx.get_config('board', 'ancor')
        self.store = store or Store(path)
        self.board = board or NativeBoard(self.board_name, profile=self.profile,
                                          env=child_environment())
        self.db_path = db_path
        self.clock = clock
        self.state_path = path.parent / (path.name + '.host.json')
        self.tasks = []

        self.coordinator = Coordinator(path.parent / 'ancor-coordinator.lock',
            pending=self.pending, snapshot=self.snapshot, scan=self.scan,
            batch_size=32, retry_base=15,
            command=lambda prompt: leader_command(self.profile, prompt))

    def state(self):
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}

    def save(self, state):
        # Called only under the coordinator's shared flock.
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state))
        temporary.replace(self.state_path)

    def scan(self):
        snapshot = self.store.snapshot()
        known = {r['id']: r for r in snapshot['requirements']}
        # Invalidation is a durable coordination event, not an automatic stop.
        # Only the leader decides whether affected native tasks should be blocked.
        try:
            self.tasks = self.board.list()
        except Exception:
            log.exception('ANCOR task discovery failed; ingesting known tasks')
            self.tasks = []
        tracked = {link['task_id'] for link in snapshot['task_links']}
        for task in self.tasks:
            link = self.board.link(task)
            if link and link['requirement_id'] in known:
                self.store.link_task(link['requirement_id'], task['id'])
                tracked.add(task['id'])
        if not tracked:
            return
        if self.db_path is None:
            from hermes_cli.kanban_db import kanban_db_path
            self.db_path = kanban_db_path(board=self.board_name)
        state = self.state()
        # Never reuse the legacy global cursor: late-discovered histories and
        # writes between independent task reads can be older than that cursor.
        cursors = state.setdefault('task_cursors', {})
        for task_id in sorted(tracked):
            try:
                events = self.board.events(self.db_path, [task_id],
                                           after=cursors.get(task_id, 0), limit=100)
            except Exception:
                log.exception('ANCOR event read failed for task %s', task_id)
                continue
            for event in events:
                if event['kind'] in {
                    'completed', 'blocked', 'review_requested', 'dependency_wait',
                    'gave_up', 'timed_out', 'spawn_failed', 'crashed', 'stale',
                }:
                    self.store.add_event('native.' + event['kind'], event,
                        source_key=f"native:{self.board_name}:{event['id']}")
                # Advance only this task after durable ingestion. Store source
                # keys make replay after a crash (or migration) harmless.
                cursors[task_id] = event['id']
            if events:
                self.save(state)

    def pending(self, limit):
        events = self.store.pending_events()[:limit]
        if not events:
            return []
        signature = hashlib.sha256(json.dumps([e['id'] for e in events]).encode()).hexdigest()
        state = self.state()
        if state.get('batch') != signature:
            state.update(batch=signature, attempts=0, retry_at=0)
        if self.clock() < state.get('retry_at', 0):
            return []
        if state.get('attempts', 0) >= 3:
            log.error('ANCOR unchanged pending batch exhausted three attempts; retry in one hour')
            state.update(attempts=0, retry_at=self.clock() + 3600)
            self.save(state)
            return []
        state['attempts'] = state.get('attempts', 0) + 1
        self.save(state)
        return events

    def snapshot(self):
        data = self.store.snapshot()
        return {'requirements': [r for r in data['requirements'] if r['status'] == 'active'],
                'task_links': data['task_links'],
                'board': [{k: t[k] for k in ('id', 'title', 'status', 'assignee', 'blocked_reason') if k in t}
                          for t in self.tasks]}


async def scan_runner(ctx, adapter):
    """Public plugin lifecycle callback; cancellation is owned by PluginContext."""
    if ctx.profile_name != ctx.get_config('owner_profile', 'default') or not ctx.get_config('gateway_owner', False):
        raise PermissionError('only the configured Gateway owner may run ANCOR')
    # Never reinterpret an agent/delegated context as trusted Gateway ownership.
    if (os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT') or
            os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_RUN_ID')):
        raise PermissionError('ANCOR host cannot run inside a native worker/delegation')
    interval = max(15, float(ctx.get_config('poll_interval', 15)))
    while not adapter.is_connected:
        await asyncio.sleep(interval)
    host = Host(ctx)
    while adapter.is_connected:
        outcome = await host.coordinator.tick()
        if outcome == 'failed':
            log.error('ANCOR coordinator infrastructure failure: %s', host.coordinator.last_error)
        await asyncio.sleep(interval)
