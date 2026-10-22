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
from .views import (actionable_notifications, collection,
                    conditions_with_state, is_terminal_task,
                    notification_counts, notification_selection,
                    notification_summary, preview, requirement_ref, task_counts,
                    task_ref)

log = logging.getLogger(__name__)

CONDITION_BACKOFF_SECONDS = 3600


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
            pending=self.pending, snapshot=self.wake_packet, scan=self.scan,
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

    def update_conditions(self, states):
        """Retain one backoff ledger entry per stalled requirement."""
        state = self.state()
        conditions = state.get('conditions')
        if not isinstance(conditions, dict):
            conditions = state['conditions'] = {}
        stalled = {item['requirement_id']: item.get('reason')
                   for item in states if item.get('state') == 'stalled'
                   and item.get('requirement_id')}
        now = self.clock()
        changed = False
        for requirement_id, reason in stalled.items():
            key = 'stalled:' + requirement_id
            current = conditions.get(key)
            if current is None:
                conditions[key] = {'reason': reason, 'since': now, 'generation': 1,
                                   'last_woken_at': None}
                changed = True
            elif current.get('reason') != reason:
                conditions[key] = {
                    'reason': reason, 'since': now,
                    'generation': int(current.get('generation') or 0) + 1,
                    'last_woken_at': None}
                changed = True
        for key in list(conditions):
            if key.startswith('stalled:') and key[8:] not in stalled:
                del conditions[key]
                changed = True
        if changed:
            self.save(state)
        return state

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
            states = self.scheduling.sync_health(snapshot, self.tasks, notifications)
            self.update_conditions(states)
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
        states = self.scheduling.sync_health(
            self.store.snapshot(), self.tasks, notifications)
        self.update_conditions(states)

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
            target = requirement.get('target_status')
            if target in {'cancelled', 'invalidated'}:
                decisions[row['id']] = 'requirement_' + target
                continue
            if target is not None:
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
            return self.board.show(task_id)
        interval = max(30, float(self.ctx.get_config('poll_interval', 30)))
        return NotificationDispatcher(
            box, NativeSender(profile), resolve_requirement=self.store.get_requirement,
            resolve_task=resolve_task, retry_seconds=interval, clock=self.clock)

    def pending(self, limit):
        if self.startup:
            # Runs under the SAME inherited coordinator flock, after discovery.
            # A pending inbox already wakes the coordinator with this current snapshot.
            data = self.wake_packet()
            pending = [e for e in self.store.pending_events() if e['kind'] != 'host.stalled']
            if (data['requirements']['items'] or data['notifications']['items']
                    or data['invalidated_tasks']['items']) and not pending:
                self.store.add_event('host.recovery', {
                    'reason': 'Reconcile durable state after host startup; inspect current context. '
                              'Reuse task keys and notification IDs; never resend sending/uncertain notices. '
                              'No automatic stop: the coordinator decides how to resolve invalidated tasks.'})
            self.startup = False
        pending = self.store.pending_events()
        state = self.state()
        conditions = state.get('conditions')
        conditions = conditions if isinstance(conditions, dict) else {}
        stalled_events = [event for event in pending if event['kind'] == 'host.stalled']
        ordinary = [event for event in pending if event['kind'] != 'host.stalled']
        now = self.clock()
        due_conditions = []
        for event in stalled_events:
            key = event.get('source_key')
            condition = conditions.get(key) if key else None
            last = condition.get('last_woken_at') if condition else None
            if condition is None or last is None or now - float(last) >= CONDITION_BACKOFF_SECONDS:
                due_conditions.append(event)
        # Reserve one slot for newest input: a full unchanged head batch must
        # not hide human replies/completions behind its hour-long cooldown.
        events = (ordinary[:limit] if len(ordinary) <= limit
                  else ordinary[:limit - 1] + ordinary[-1:])
        # Reserve slots for due stalled conditions and liveness even behind a
        # large input backlog. A recovering condition simply disappears.
        reserved = due_conditions + [
            next((row for row in pending if row['kind'] == 'host.liveness'), None)]
        reserved = [event for event in reserved if event is not None]
        missing = [event for event in reserved if event not in events]
        if missing:
            events = events[:max(0, limit - len(missing))] + missing
            events = events[:limit]
        if not events:
            return []
        signature = hashlib.sha256(json.dumps([e['id'] for e in events]).encode()).hexdigest()
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
        for event in events:
            if event['kind'] == 'host.stalled' and event.get('source_key') in conditions:
                conditions[event['source_key']]['last_woken_at'] = now
        self.save(state)
        return events

    def invalidated_tasks(self, data):
        """Surface residual drift after an invalidated requirement should be terminal."""
        invalidated = {r['id']: r for r in data['requirements'] if r['status'] == 'invalidated'}
        tasks = {t['id']: t for t in self.tasks}
        unresolved = []
        for link in data['task_links']:
            rid, tid = link['requirement_id'], link['task_id']
            if rid not in invalidated:
                continue
            task = tasks.get(tid, {})
            status = task.get('status')
            if status == 'archived':
                continue
            reason = ('Invalidated requirement has a linked native task that is not archived'
                      if status else 'Invalidated requirement linked task is unreadable')
            shown, truncated = preview(reason, 160)
            item = {
                'task_id': tid,
                'task_title': task.get('title'),
                'task_status': status,
                'requirement_id': rid,
                'requirement_title': invalidated[rid]['title'],
                'reason_preview': shown,
            }
            if truncated:
                item['reason_truncated'] = True
            unresolved.append(item)
        return unresolved

    def wake_packet(self):
        from .notification import NotificationOutbox
        data = self.store.snapshot()
        notifications = NotificationOutbox.read_existing(self.store.path + '.notifications.db')
        delivered = {n['id'] for n in notifications
                     if n['state'] == 'sent' and n['kind'] == 'completion'}
        requirements = [r for r in data['requirements']
                        if r['status'] in {'active', 'paused', 'pending'}
                        or r.get('target_status') is not None or
                        (r['status'] == 'accepted' and 'accept:' + r['id'] not in delivered)]
        relevant = {r['id'] for r in requirements}
        scoped = {**data, 'requirements': requirements}
        observation = self.scheduling.observation(scoped, self.tasks, notifications)
        progression = {item['requirement_id']: item for item in observation['progression']}
        titles = {r['id']: r['title'] for r in data['requirements']}
        links = {link['task_id']: link['requirement_id'] for link in data['task_links']
                 if link['requirement_id'] in relevant}
        linked_tasks = [task for task in self.tasks if task['id'] in links]
        board = [task_ref(task, links[task['id']], titles.get(links[task['id']]))
                 for task in linked_tasks if not is_terminal_task(task)]
        visible_notifications = actionable_notifications(notifications)
        selected = notification_selection(visible_notifications, 10)
        condition_state = self.state().get('conditions')
        conditions = conditions_with_state(
            observation['progression'],
            condition_state if isinstance(condition_state, dict) else {})
        for condition in conditions:
            condition['requirement_title'] = titles.get(condition['requirement_id'])
        return {
            'requirements': collection(
                [requirement_ref(r, progression.get(r['id'])) for r in requirements]),
            'invalidated_tasks': collection(self.invalidated_tasks(data)),
            'conditions': collection(conditions),
            'notifications': collection(
                [notification_summary(row) for row in selected],
                total=len(visible_notifications)),
            'notification_counts': notification_counts(notifications),
            'board': collection(board),
            'board_counts': task_counts(linked_tasks),
            'scheduled_wakes': observation['scheduled_wakes'],
            'progression': observation['progression'],
            'liveness': observation['liveness'],
            'expand': 'Call ancor_get_context for requirement detail, or ancor_board show for one card.',
        }


async def scan_runner(ctx, adapter):
    """Public plugin lifecycle callback; cancellation is owned by PluginContext."""
    if ctx.profile_name != ctx.get_config('owner_profile', 'default') or not ctx.get_config('gateway_owner', False):
        raise PermissionError('only the configured Gateway owner may run ANCOR')
    # Never reinterpret an agent/delegated context as trusted Gateway ownership.
    if (os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT') or
            os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_RUN_ID')):
        raise PermissionError('ANCOR host cannot run inside a native worker/delegation')
    interval = max(30, float(ctx.get_config('poll_interval', 30)))
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
