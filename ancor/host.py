"""Gateway-owned polling bridge; no model calls without durable pending work.

Native boundary: adapter.is_connected, NativeBoard CLI, and read-only native
list_events/kanban_db_path. No native SQL or guard overrides. Retry budget is
persisted beside the store: three attempts per event, then a one-hour
cooldown (not an acknowledgement). New events can run immediately.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
import time

from .board import NativeBoard
from .event_policy import is_wake_event, payload_dict
from .execution_guard import (freeze_execution_unknown, is_execution_unknown,
                              is_execution_unknown_hold)
from .runtime import Coordinator
from .views import completion_id
from .store import Store
from .views import (actionable_notifications, collection,
                    conditions_with_state, is_terminal_task,
                    notification_counts, notification_selection,
                    notification_summary, preview, request_ref, task_counts,
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
        """Retain one backoff ledger entry per stalled request."""
        state = self.state()
        conditions = state.get('conditions')
        if not isinstance(conditions, dict):
            conditions = state['conditions'] = {}
        stalled = {item['request_id']: item.get('reason')
                   for item in states if item.get('state') == 'stalled'
                   and item.get('request_id')}
        now = self.clock()
        changed = False
        for request_id, reason in stalled.items():
            key = 'stalled:' + request_id
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
        known = {r['id']: r for r in snapshot['requests']}
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
            if link and link['request_id'] in known:
                self.store.link_task(link['request_id'], task['id'])
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
                details = payload_dict(event)
                unknown = (event['kind'] == 'crashed'
                           and is_execution_unknown(details))
                if unknown:
                    # The owner hook normally freezes before the next dispatch.
                    # Repeating the guard here covers plugin reloads, older
                    # dispatchers, and a hook write that raced with shutdown.
                    try:
                        freeze_execution_unknown(
                            task_id, board=self.board_name, run_id=event.get('run_id'))
                    except Exception:
                        log.exception('ANCOR execution freeze failed for task %s', task_id)
                    event = {
                        **event,
                        'payload': {
                            **details,
                            'ancor_classification': 'execution_unknown',
                            'reconcile_before_retry': True,
                        },
                    }
                    self.store.add_event(
                        'native.execution_unknown', event,
                        source_key=f"native:{self.board_name}:{event['id']}")
                elif not is_execution_unknown_hold(event) and is_wake_event(event):
                    # Heartbeats and bookkeeping reach this cursor only as the
                    # ignored page watermark; they never create model work.
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
        """Close questions and completion notices whose structural reason disappeared."""
        from .notification import NotificationOutbox
        path = Path(self.store.path + '.notifications.db')
        if not path.exists() and not self.store.questions():
            return []
        snapshot = snapshot or self.store.snapshot()
        requests = {row['id']: row for row in snapshot['requests']}
        tasks = {task['id']: task for task in self.tasks}
        box = self.notification_outbox()
        if box is None:
            return NotificationOutbox.read_existing(path)
        self.store.sync_questions(box)
        managed = {q['id']: q for q in self.store.questions()}
        decisions = {}
        for row in box.list():
            if row['kind'] != 'question' or row['answer'] is not None or row['closed_at'] is not None:
                continue
            request = requests.get(row.get('request_id'))
            if request is None:
                decisions[row['id']] = 'request_missing'
                continue
            target = request.get('target_status')
            if target in {'cancelled', 'invalidated'}:
                decisions[row['id']] = 'request_' + target
                continue
            if target is not None:
                continue
            status = request['status']
            if status in {'cancelled', 'invalidated', 'accepted'}:
                decisions[row['id']] = 'request_' + status
                continue
            task = tasks.get(row.get('task_id'))
            if task is not None and task.get('status') != 'blocked':
                decisions[row['id']] = 'task_not_blocked'
                if row['id'] in managed:
                    self.store.withdraw_question(row['id'], 'task_not_blocked', 'host')
        if decisions:
            box.close_questions(decisions, closed_by='host')
            log.info('ANCOR closed %d stale question(s): %s', len(decisions),
                     ', '.join(f'{key}={value}' for key, value in sorted(decisions.items())))
        completion_requests = {}
        orphaned_completions = {}
        for row in box.list():
            if (row['kind'] != 'completion' or row.get('closed_at') is not None):
                continue
            request = requests.get(row.get('request_id'))
            if request is None:
                if row.get('request_id'):
                    completion_requests.setdefault(
                        row.get('request_id'), 'request_missing')
                else:
                    orphaned_completions[row['id']] = 'request_missing'
            elif request.get('content_mode') == 'points' and row['id'] != completion_id(request):
                orphaned_completions[row['id']] = 'verification_superseded'
            elif request.get('status') != 'awaiting_acceptance' or request.get('target_status'):
                completion_requests.setdefault(
                    row.get('request_id'), 'completion_no_longer_pending')
        for notification_id, reason in orphaned_completions.items():
            box.close_delivery(notification_id, closed_by='host', reason=reason)
        for request_id, reason in completion_requests.items():
            if request_id:
                box.close_completions(request_id, closed_by='host', reason=reason)
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
            box, NativeSender(profile), resolve_request=self.store.get_request,
            resolve_task=resolve_task, retry_seconds=interval, clock=self.clock)

    def pending(self, limit):
        if self.startup:
            # Runs under the SAME inherited coordinator flock, after discovery.
            # A pending inbox already wakes the coordinator with this current snapshot.
            data = self.wake_packet()
            pending = [e for e in self.store.pending_events() if e['kind'] != 'host.stalled']
            if (data['requests']['items'] or data['notifications']['items']
                    or data['invalidated_tasks']['items']) and not pending:
                self.store.add_event('host.recovery', {
                    'reason': 'Reconcile durable state after host startup; inspect current context. '
                              'Reuse task keys and notification IDs; never resend sending/uncertain notices. '
                              'No automatic stop: the coordinator decides how to resolve invalidated tasks.'})
            self.startup = False
        pending = self.store.pending_events()
        for work in self.store.pending_coordination():
            key = f"coordination:{work['id']}:{work['generation']}"
            existing = next((e for e in pending if e.get('source_key') == key), None)
            if existing is None:
                event = self.store.add_event('request.reconcile_needed', {
                    'request_id': work['id'], 'generation': work['generation']}, source_key=key)
                if event['acknowledged']:
                    with self.store._transaction() as db:
                        db.execute('UPDATE events SET acknowledged=0 WHERE id=?', (event['id'],))
        pending = self.store.pending_events()
        state = self.state()
        conditions = state.get('conditions')
        conditions = conditions if isinstance(conditions, dict) else {}
        stalled_events = [event for event in pending if event['kind'] == 'host.stalled']
        ordinary = [event for event in pending if event['kind'] != 'host.stalled']
        now = self.clock()
        # Each event keeps its budget when another event joins or leaves the batch.
        retries = state.setdefault('event_retries', {})
        pending_ids = {str(event['id']) for event in ordinary}
        for key in list(retries):
            if key not in pending_ids:
                del retries[key]
        for key in ('batch', 'attempts', 'retry_at'):
            state.pop(key, None)
        eligible = []
        for event in ordinary:
            retry = retries.setdefault(str(event['id']), {'attempts': 0, 'retry_at': 0})
            if now < retry['retry_at']:
                continue
            if retry['attempts'] >= 3:
                retry.update(attempts=0, retry_at=now + 3600)
                log.warning('ANCOR pending event %s exhausted three attempts; retry in one hour', event['id'])
                continue
            eligible.append(event)
        ordinary = eligible
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
            next((row for row in ordinary if row['kind'] == 'host.liveness'), None)]
        reserved = [event for event in reserved if event is not None]
        missing = [event for event in reserved if event not in events]
        if missing:
            events = events[:max(0, limit - len(missing))] + missing
            events = events[:limit]
        for event in events:
            if event['kind'] != 'host.stalled':
                retries[str(event['id'])]['attempts'] += 1
            if event['kind'] == 'host.stalled' and event.get('source_key') in conditions:
                conditions[event['source_key']]['last_woken_at'] = now
        self.save(state)
        return events

    def invalidated_tasks(self, data):
        """Surface residual drift after an invalidated request should be terminal."""
        invalidated = {r['id']: r for r in data['requests'] if r['status'] == 'invalidated'}
        tasks = {t['id']: t for t in self.tasks}
        unresolved = []
        for link in data['task_links']:
            rid, tid = link['request_id'], link['task_id']
            if rid not in invalidated:
                continue
            task = tasks.get(tid, {})
            status = task.get('status')
            if status == 'archived':
                continue
            reason = ('Invalidated request has a linked native task that is not archived'
                      if status else 'Invalidated request linked task is unreadable')
            shown, truncated = preview(reason, 160)
            item = {
                'task_id': tid,
                'task_title': task.get('title'),
                'task_status': status,
                'request_id': rid,
                'request_title': invalidated[rid]['title'],
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
        pending_completion = {n['request_id'] for n in notifications
                              if n['kind'] == 'completion'
                              and n.get('closed_at') is None
                              and n.get('request_id')}
        requests = [r for r in data['requests']
                        if r['status'] in {'active', 'paused', 'pending'}
                        or r.get('target_status') is not None or
                        (r['status'] == 'accepted' and 'accept:' + r['id'] not in delivered)
                        or (r['status'] == 'awaiting_acceptance'
                            and r['id'] not in pending_completion)]
        relevant = {r['id'] for r in requests}
        scoped = {**data, 'requests': requests}
        observation = self.scheduling.observation(scoped, self.tasks, notifications)
        progression = {item['request_id']: item for item in observation['progression']}
        titles = {r['id']: r['title'] for r in data['requests']}
        links = {link['task_id']: link['request_id'] for link in data['task_links']
                 if link['request_id'] in relevant}
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
            condition['request_title'] = titles.get(condition['request_id'])
        return {
            'requests': collection(
                [request_ref(r, progression.get(r['id'])) for r in requests]),
            'invalidated_tasks': collection(self.invalidated_tasks(data)),
            'pending_coordination': collection(self.store.pending_coordination()),
            'questions': collection([{'id': q['id'], 'request_id': q['request_id'],
                'task_id': q['task_id'], 'revision': q['revision'], 'state': q['state']}
                for q in self.store.questions() if q['state'] == 'open']),
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
            'expand': 'Call ancor_get_context for request detail, or ancor_board show for one card.',
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
