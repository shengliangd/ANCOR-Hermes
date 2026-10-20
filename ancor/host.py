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


def coordinator_command(profile, prompt):
    # env -u affects only the child, never concurrent Gateway sessions. Explicit
    # -p is resolved by the native CLI before importing profile-scoped modules.
    clean = child_environment()
    removed = sorted(set(os.environ) - set(clean))
    return ['env', *[part for key in removed for part in ('-u', key)],
            'hermes', '-p', profile, '--cli', 'chat', '-q', prompt]





class Host:
    def __init__(self, ctx, *, board=None, store=None, db_path=None, clock=time.time):
        path = Path(ctx.get_config('store_path', ''))
        if not path.is_absolute():
            raise ValueError('configure shared absolute store_path for ANCOR')
        self.ctx = ctx
        self.profile = ctx.get_config('coordinator_profile', 'ancor-coordinator')
        self.board_name = ctx.get_config('board', 'ancor')
        self.store = store or Store(path)
        self.board = board or NativeBoard(self.board_name, profile=self.profile,
                                          env=child_environment())
        self.db_path = db_path
        self.clock = clock
        from .scheduling import Scheduling
        self.scheduling = Scheduling(self.store, clock)
        self.state_path = path.parent / (path.name + '.host.json')
        self.tasks = []
        self.startup = True

        self.coordinator = Coordinator(path.parent / 'ancor-coordinator.lock',
            pending=self.pending, snapshot=self.snapshot, scan=self.scan,
            batch_size=32, retry_base=15,
            command=lambda prompt: coordinator_command(self.profile, prompt))

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
        self.scheduling.scan()
        snapshot = self.store.snapshot()
        known = {r['id']: r for r in snapshot['requirements']}
        # Invalidation is a durable coordination event, not an automatic stop.
        # Only the coordinator decides whether affected native tasks should be blocked.
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
        notifications = self.close_stale_notifications(snapshot)
        if not tracked:
            self.scheduling.sync_health(snapshot, self.tasks, notifications)
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
                # Outcome/anomaly events, not every native state mutation. In
                # particular respawn_guarded repeats every dispatcher tick, while
                # rate_limited records an actual ended attempt. Comments, edits,
                # heartbeats and scheduling bookkeeping must not self-wake.
                # Native auto_blocked is a DispatchResult field; its durable
                # circuit-breaker event is gave_up (not auto_blocked).
                if event['kind'] in {
                    'completed', 'blocked', 'review_requested', 'dependency_wait',
                    'gave_up', 'timed_out', 'spawn_failed', 'crashed', 'stale',
                    'rate_limited', 'block_loop_detected', 'protocol_violation',
                    'completion_blocked_hallucination', 'completion_blocked_empty_result',
                    'suspected_hallucinated_references', 'changes_requested',
                    'descendant_invalidated',
                }:
                    self.store.add_event('native.' + event['kind'], event,
                        source_key=f"native:{self.board_name}:{event['id']}")
                # Advance only this task after durable ingestion. Store source
                # keys make replay after a crash (or migration) harmless.
                cursors[task_id] = event['id']
            if events:
                self.save(state)
        self.scheduling.sync_health(self.store.snapshot(), self.tasks, notifications)

    def notification_outbox(self):
        from .notification import NotificationOutbox
        target = self.ctx.get_config('human_target')
        if target is not None and (not isinstance(target, str) or ':' not in target
                                   or not target.partition(':')[2].strip()):
            return None
        return NotificationOutbox(
            self.store.path + '.notifications.db',
            coordinator_profile=self.profile, target=target, clock=self.clock)

    def close_stale_notifications(self, snapshot=None):
        """Close unanswered questions whose structural reason has disappeared."""
        from .notification import NotificationOutbox
        path = Path(self.store.path + '.notifications.db')
        if not path.exists():
            return []
        snapshot = snapshot or self.store.snapshot()
        requirements = {row['id']: row for row in snapshot['requirements']}
        tasks = {task['id']: task for task in self.tasks}
        box = self.notification_outbox()
        if box is None:
            return NotificationOutbox.read_existing(path)
        decisions = {}
        for row in box.list():
            if row['kind'] != 'question' or row['answer'] is not None or row['closed_at'] is not None:
                continue
            requirement = requirements.get(row.get('requirement_id'))
            if requirement is None:
                decisions[row['id']] = 'requirement_missing'
                continue
            status = requirement['status']
            if status in {'cancelled', 'invalidated', 'accepted'}:
                decisions[row['id']] = 'requirement_' + status
                continue
            task = tasks.get(row.get('task_id'))
            if task is not None and task.get('status') != 'blocked':
                decisions[row['id']] = 'task_not_blocked'
        if decisions:
            box.close_questions(decisions, closed_by='host')
            log.info('ANCOR closed %d stale question(s): %s', len(decisions),
                     ', '.join(f'{key}={value}' for key, value in sorted(decisions.items())))
        return box.list()

    def notification_dispatcher(self):
        from .notification import NativeSender, NotificationDispatcher
        box = self.notification_outbox()
        profile = self.ctx.get_config('transport_profile')
        if box is None or not isinstance(profile, str) or not profile.strip():
            raise ValueError('human_target and transport_profile required for notification delivery')
        def resolve_task(task_id):
            if not task_id:
                return None
            task = next((row for row in self.tasks if row['id'] == task_id), None)
            if task is not None:
                return task
            try:
                return self.board.show(task_id)
            except Exception:
                return None
        interval = max(15, float(self.ctx.get_config('poll_interval', 15)))
        return NotificationDispatcher(
            box, NativeSender(profile), resolve_requirement=self.store.get_requirement,
            resolve_task=resolve_task, retry_seconds=interval, clock=self.clock)

    def pending(self, limit):
        if self.startup:
            # Runs under the SAME inherited coordinator flock, after discovery.
            # A pending inbox already wakes the coordinator with this current snapshot.
            data = self.snapshot()
            pending = [e for e in self.store.pending_events() if e['kind'] != 'host.stalled']
            if (data['requirements'] or data['notifications'] or data['invalidated_tasks']) and not pending:
                self.store.add_event('host.recovery', {
                    'reason': 'Reconcile durable state after host startup; inspect current context. '
                              'Reuse task keys and notification IDs; never resend sending/uncertain notices. '
                              'No automatic stop: the coordinator decides how to resolve invalidated tasks.'})
            self.startup = False
        pending = self.store.pending_events()
        # Reserve one slot for newest input: a full unchanged head batch must
        # not hide human replies/completions behind its hour-long cooldown.
        events = pending[:limit] if len(pending) <= limit else pending[:limit - 1] + pending[-1:]
        # Reserve slots for the two host-owned conditions even behind a large
        # input backlog: stalled work is more important than ordinary events.
        reserved = [e for kind in ('host.stalled', 'host.liveness')
                    for e in [next((row for row in pending if row['kind'] == kind), None)] if e]
        missing = [event for event in reserved if event not in events]
        if missing:
            events = events[:max(0, limit - len(missing))] + missing
            events = events[:limit]
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

    def invalidated_tasks(self, data):
        """Surface obsolete unfinished work, never stop it or infer retirement."""
        from .operations import Operations
        from .retirement import Retirement

        invalidated = {r['id']: r for r in data['requirements'] if r['status'] == 'invalidated'}
        tasks = {t['id']: t for t in self.tasks}
        retired = {}
        unresolved = []
        for link in data['task_links']:
            rid, tid = link['requirement_id'], link['task_id']
            if rid not in invalidated:
                continue
            task = tasks.get(tid, {})
            status = task.get('status')
            if status in {'todo', 'ready', 'running', 'review', 'triage'}:
                reason = 'Invalidated requirement still has unfinished ' + status + ' work'
            else:
                try:
                    detail = self.board._call('show', tid)
                    Operations.require_quiescence(detail)
                    if any(not run.get('ended_at') for run in detail['runs']):
                        raise RuntimeError('active run')
                    if detail['task']['status'] == 'done':
                        continue
                    if rid not in retired:
                        retired[rid] = Retirement(self.ctx, self.store, self.board).effective_retired(rid)
                    if tid in retired[rid]:
                        continue
                    reason = 'Invalidated requirement has unresolved work without safe retirement'
                except Exception:
                    # Failed reads are not evidence that obsolete work is quiet.
                    reason = 'Invalidated task quiescence could not be established; inspect native detail'
            unresolved.append({'requirement': invalidated[rid], 'task_id': tid, 'reason': reason})
        return unresolved

    def snapshot(self):
        from .notification import NotificationOutbox
        data = self.store.snapshot()
        notifications = NotificationOutbox.read_existing(self.store.path + '.notifications.db')
        delivered = {n['id'] for n in notifications
                     if n['state'] == 'sent' and n['kind'] == 'completion'}
        requirements = [r for r in data['requirements']
                        if r['status'] in {'active', 'paused'} or
                        (r['status'] == 'accepted' and 'accept:' + r['id'] not in delivered)]
        relevant = {r['id'] for r in requirements}
        return {**self.scheduling.observation(data, self.tasks, notifications), 'requirements': requirements,
                'invalidated_tasks': self.invalidated_tasks(data),
                'acceptances': [e for e in self.store.acceptances()
                                if e['payload']['requirement_id'] in relevant],
                'notifications': [n for n in notifications if n['state'] != 'sent' or
                                  (n['kind'] == 'question' and n['answer'] is None
                                   and n.get('closed_at') is None)],
                'task_links': data['task_links'],
                'board': [{k: t[k] for k in ('id', 'title', 'status', 'assignee', 'blocked_reason',
                                             'last_heartbeat_at', 'block_kind', 'block_recurrences',
                                             'created_at', 'started_at') if k in t}
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
    delivery = None
    try:
        try:
            dispatcher = host.notification_dispatcher()
        except ValueError as exc:
            log.error('ANCOR notification delivery unavailable: %s', exc)
        else:
            delivery = asyncio.create_task(
                dispatcher.run(lambda: adapter.is_connected), name='ancor:notifications')
        while adapter.is_connected:
            tick = asyncio.create_task(host.coordinator.tick())
            try:
                while not tick.done():
                    done, _ = await asyncio.wait({tick}, timeout=interval)
                    if not done and host.scheduling.observation(host.store.snapshot())['liveness']['overdue']:
                        log.error('ANCOR liveness overdue: coordinator still running; no overlapping launch')
                outcome = await tick
            finally:
                if not tick.done():
                    tick.cancel()
                    try:
                        await tick
                    except asyncio.CancelledError:
                        pass
            if outcome == 'busy':
                observation = host.scheduling.observation(host.store.snapshot())
                if observation['liveness']['overdue']:
                    log.error('ANCOR liveness overdue: coordinator lock remains held; inspect stuck/surviving coordinator, no overlapping launch')
            if outcome == 'failed':
                log.error('ANCOR coordinator infrastructure failure: %s', host.coordinator.last_error)
            await asyncio.sleep(interval)
    finally:
        if delivery is not None:
            delivery.cancel()
            try:
                await delivery
            except asyncio.CancelledError:
                pass
