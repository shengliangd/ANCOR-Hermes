"""Gateway-owned thin deterministic host: Reconciler, Watcher, GoalRunner, Root Agent.

2-profile architecture (Plan A):
  - ``default``: user's everyday profile (Interactor + Gateway owner)
  - ``ancor-exec``: single headless execution profile; role is selected per
    process via ``ANCOR_ROLE=root|goal|judge`` (plus ``GOAL_ID`` for goal nodes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import time

from .goals import ACTIVE_HANDLE_STATUSES, GoalStore
from .judge import DEFAULT_MAX_JUDGE_REJECTS, JudgeGate
from .model_routing import CategoryRouter
from .runtime import Coordinator
from .store import Store
from .views import (actionable_notifications, collection, completion_id,
                    notification_counts, notification_selection,
                    notification_summary, request_ref)
from .watcher import FileClusterBackend, GoalReconciler, GoalRunner, GoalWatcher

log = logging.getLogger(__name__)

CONDITION_BACKOFF_SECONDS = 3600


def child_environment(environ=None):
    """Drop routing identity only; retain ALL native delegation fences."""
    source = dict(os.environ if environ is None else environ)
    return {k: v for k, v in source.items()
            if not k.startswith('HERMES_SESSION_') and k not in {
                'HERMES_UI_SESSION_ID', 'HERMES_BROWSER_CONTROL_PRINCIPAL',
                'HERMES_BROWSER_CONTROL_TRANSPORT_FAMILY', 'HERMES_CRON_SESSION'}}


def _exec_role_command(role: str, exec_profile: str, prompt: str, model_args=()):
    clean = child_environment()
    removed = sorted((set(os.environ) - set(clean)) | {'GOAL_ID', 'ANCOR_GOAL_ID'})
    return ['env', *[part for key in removed for part in ('-u', key)],
            f'ANCOR_ROLE={role}',
            'hermes', '-p', exec_profile, '--cli', 'chat', *model_args, '-q', prompt]


def root_command(exec_profile: str, prompt: str, model_args=()):
    """Launch Root Agent under ancor-exec with ANCOR_ROLE=root (no GOAL_ID).

    model_args is ``['-m', 'root']`` when ancor-exec defines a ``root`` alias, else empty.
    """
    return _exec_role_command('root', exec_profile, prompt, model_args)


def judge_command(exec_profile: str, prompt: str, model_args=()):
    """Launch independent Judge Agent under ancor-exec with ANCOR_ROLE=judge (clean context)."""
    return _exec_role_command('judge', exec_profile, prompt, model_args)


def goal_summary(goal: dict) -> dict:
    """Compact summary of one active_goals node for Root Agent wake packets."""
    return {
        'goal_id': goal['goal_id'],
        'parent_goal_id': goal['parent_goal_id'],
        'request_id': goal['request_id'],
        'generation': goal['generation'],
        'status': goal['status'],
        'paused_by_ancestor': goal['paused_by_ancestor'],
        'objective': goal['objective'][:160],
        'budget': goal['budget'],
        'category': goal.get('category'),
        'observed_models': goal.get('observed_models') or [],
        'active_handles': sum(
            1 for h in goal['external_job_ids']
            if h.get('status', 'running') in ACTIVE_HANDLE_STATUSES
        ),
        'needs_wake': goal['needs_wake'],
        'pid': goal['pid'],
    }


class Host:
    def __init__(self, ctx, *, store=None, cluster_backend=None, clock=time.time,
                 goal_command_builder=None, coordinator_cmd=None, category_router=None,
                 judge_launcher=None):
        path = Path(ctx.get_config('store_path', ''))
        if not path.is_absolute():
            raise ValueError('configure shared absolute store_path for ANCOR')
        self.ctx = ctx
        self.exec_profile = ctx.get_config('exec_profile', 'ancor-exec')
        self.store = store or Store(path)
        self.clock = clock
        self.state_path = path.parent / (path.name + '.host.json')
        cluster_path = ctx.get_config('cluster_state_path')
        self.cluster = cluster_backend or FileClusterBackend(
            Path(cluster_path) if cluster_path else path.parent / 'cluster.json',
            clock=clock)
        self.router = category_router or CategoryRouter.from_ctx(ctx)
        self.goals = GoalStore(
            self.store, clock=clock,
            global_limits=ctx.get_config('global_limits'),
            category_router=self.router)
        self.watcher = GoalWatcher(self.goals, self.cluster, clock=clock)
        self.reconciler = GoalReconciler(self.goals, self.cluster, clock=clock)
        self.runner = GoalRunner(
            self.goals, profile=self.exec_profile,
            command_builder=goal_command_builder,
            env_builder=child_environment)
        self.judge = JudgeGate(
            self.store, self.goals,
            max_rejects=ctx.get_config('max_judge_rejects', DEFAULT_MAX_JUDGE_REJECTS),
            clock=clock)
        self.judge_launcher = judge_launcher or self._launch_judge_process
        self.startup = True
        self.last_reconcile = None
        self.last_watch = None

        event_batch_size = ctx.get_config('event_batch_size', 12)
        if type(event_batch_size) is not int or not 1 <= event_batch_size <= 32:
            raise ValueError('event_batch_size must be an integer between 1 and 32')

        cmd = coordinator_cmd or (lambda prompt: root_command(
            self.exec_profile, prompt, self.router.cli_args(self.router.role_category('root'))))
        self.coordinator = Coordinator(
            path.parent / 'ancor-root.lock',
            pending=self.pending, snapshot=self.wake_packet, scan=self.scan,
            batch_size=event_batch_size, retry_base=15, command=cmd)

    def state(self):
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}

    def save(self, state):
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state))
        temporary.replace(self.state_path)

    def scan(self):
        """Deterministic scan: crash-safe startup reconcile + zero-LLM Watcher + stale notices."""
        if self.startup:
            self.last_reconcile = self.reconciler.reconcile()
        self.last_watch = self.watcher.poll()
        snapshot = self.store.snapshot()
        self.close_stale_notifications(snapshot)

    def judge_command_for(self, packet: dict) -> list[str]:
        prompt = (
            'You are the independent ANCOR delivery Judge. Review this delivery packet against the '
            'authoritative requirement (read it with ancor_request_read), then call ancor_judge_verdict '
            'exactly once with accept, reject (with structured feedback), or escalate.\n'
            + json.dumps(packet, ensure_ascii=False, separators=(',', ':')))
        return judge_command(self.exec_profile, prompt,
                             self.router.cli_args(self.router.role_category('judge')))

    async def _launch_judge_process(self, packet: dict) -> int:
        proc = await asyncio.create_subprocess_exec(
            *self.judge_command_for(packet),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        return await proc.wait()

    async def dispatch_judges(self) -> list[int]:
        """Deterministically hand each delivery.submitted event to a fresh Judge process."""
        events = [e for e in self.store.pending_events() if e['kind'] == 'delivery.submitted']
        if not events:
            return []
        self.store.ack_events([e['id'] for e in events])
        launched = []
        for event in events:
            packet = (event.get('payload') or {}).get('judge_packet')
            if not packet:
                log.error('ANCOR delivery.submitted event %s has no judge_packet', event['id'])
                continue
            try:
                await self.judge_launcher(packet)
                launched.append(event['id'])
            except Exception:
                log.exception('ANCOR Judge launch failed for event %s', event['id'])
        return launched

    def notification_outbox(self):
        from .notification import NotificationOutbox
        target = self.ctx.get_config('human_target')
        if target is not None and (not isinstance(target, str) or ':' not in target
                                   or not target.partition(':')[2].strip()):
            return None
        return NotificationOutbox(
            self.store.path + '.notifications.db',
            coordinator_profile=self.exec_profile, target=target, clock=self.clock)

    def close_stale_notifications(self, snapshot=None):
        """Close questions and completion notices whose structural reason disappeared."""
        from .notification import NotificationOutbox
        path = Path(self.store.path + '.notifications.db')
        if not path.exists() and not self.store.questions():
            return []
        snapshot = snapshot or self.store.snapshot()
        requests = {row['id']: row for row in snapshot['requests']}
        box = self.notification_outbox()
        if box is None:
            return NotificationOutbox.read_existing(path)
        self.store.sync_questions(box)
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
        if decisions:
            box.close_questions(decisions, closed_by='host')
        completion_requests = {}
        orphaned_completions = {}
        for row in box.list():
            if row['kind'] != 'completion' or row.get('closed_at') is not None:
                continue
            request = requests.get(row.get('request_id'))
            if request is None:
                if row.get('request_id'):
                    completion_requests.setdefault(row.get('request_id'), 'request_missing')
                else:
                    orphaned_completions[row['id']] = 'request_missing'
            elif request.get('content_mode') == 'points' and row['id'] != completion_id(request):
                orphaned_completions[row['id']] = 'verification_superseded'
            elif request.get('status') != 'awaiting_acceptance' or request.get('target_status'):
                completion_requests.setdefault(row.get('request_id'), 'completion_no_longer_pending')
        for notification_id, reason in orphaned_completions.items():
            box.close_delivery(notification_id, closed_by='host', reason=reason)
        for request_id, reason in completion_requests.items():
            if request_id:
                box.close_completions(request_id, closed_by='host', reason=reason)
        return box.list()

    def notification_dispatcher(self):
        from .notification import NativeSender, NotificationDispatcher
        box = self.notification_outbox()
        profile = self.ctx.get_config('transport_profile', 'default')
        if box is None or not isinstance(profile, str) or not profile.strip():
            raise ValueError('human_target and transport_profile required for notification delivery')
        interval = max(30, float(self.ctx.get_config('poll_interval', 30)))
        return NotificationDispatcher(
            box, NativeSender(profile), resolve_request=self.store.get_request,
            resolve_task=lambda _tid: None, retry_seconds=interval, clock=self.clock)

    def pending(self, limit):
        if self.startup:
            self.startup = False
        pending = self.store.pending_events()
        for work in self.store.pending_coordination():
            key = f"coordination:{work['id']}:{work['generation']}"
            existing = next((e for e in pending if e.get('source_key') == key), None)
            if existing is None:
                event = self.store.add_event('request.generation_changed', {
                    'request_id': work['id'], 'generation': work['generation'],
                    'observed_generation': work['observed_generation']}, source_key=key)
                if event['acknowledged']:
                    with self.store._transaction() as db:
                        db.execute('UPDATE events SET acknowledged=0 WHERE id=?', (event['id'],))
        # delivery.submitted belongs to the Judge dispatcher, never to Root.
        pending = [e for e in self.store.pending_events() if e['kind'] != 'delivery.submitted']
        state = self.state()
        now = self.clock()
        retries = state.setdefault('event_retries', {})
        pending_ids = {str(event['id']) for event in pending}
        for key in list(retries):
            if key not in pending_ids:
                del retries[key]
        eligible = []
        for event in pending:
            retry = retries.setdefault(str(event['id']), {'attempts': 0, 'retry_at': 0})
            if now < retry['retry_at']:
                continue
            if retry['attempts'] >= 3:
                retry.update(attempts=0, retry_at=now + 3600)
                log.warning('ANCOR pending event %s exhausted three attempts; retry in one hour', event['id'])
                continue
            eligible.append(event)
        events = (eligible[:limit] if len(eligible) <= limit
                  else eligible[:limit - 1] + eligible[-1:])
        for event in events:
            retries[str(event['id'])]['attempts'] += 1
        self.save(state)
        return events

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
        requests = [
            r for r in data['requests']
            if r['status'] in {'active', 'paused', 'pending'}
            or r.get('target_status') is not None
            or (r['status'] == 'accepted' and 'accept:' + r['id'] not in delivered)
            or (r['status'] == 'awaiting_acceptance' and r['id'] not in pending_completion)
        ]
        active_goals = self.goals.list_goals(include_terminal=False)
        visible_notifications = actionable_notifications(notifications)
        selected = notification_selection(visible_notifications, 10)
        return {
            'requests': collection([request_ref(r) for r in requests]),
            'active_goals': collection([goal_summary(g) for g in active_goals]),
            'resources': {
                **self.goals.total_active_usage(),
                'backend': self.cluster.summary(),
            },
            'pending_coordination': collection(self.store.pending_coordination()),
            'model_categories': sorted(self.router.alias_names() or []),
            'questions': collection([
                {'id': q['id'], 'request_id': q['request_id'],
                 'revision': q['revision'], 'state': q['state']}
                for q in self.store.questions() if q['state'] == 'open'
            ]),
            'notifications': collection(
                [notification_summary(row) for row in selected],
                total=len(visible_notifications)),
            'notification_counts': notification_counts(notifications),
            'expand': 'Call ancor_request_get for request detail, or ancor_goal_get for one goal.',
        }


async def scan_runner(ctx, adapter):
    """Public plugin lifecycle callback; cancellation is owned by PluginContext."""
    if ctx.profile_name != ctx.get_config('owner_profile', 'default') or not ctx.get_config('gateway_owner', False):
        raise PermissionError('only the configured Gateway owner may run ANCOR')
    if os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT') or os.environ.get('ANCOR_ROLE') or os.environ.get('GOAL_ID'):
        raise PermissionError('ANCOR host cannot run inside a delegated or execution child process')
    interval = max(5, float(ctx.get_config('poll_interval', 15)))
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
            await asyncio.to_thread(host.scan)
            await host.runner.dispatch_ready()
            await host.dispatch_judges()
            outcome = await host.coordinator.tick()
            if outcome == 'failed':
                log.error('ANCOR Root Agent infrastructure failure: %s', host.coordinator.last_error)
            await asyncio.sleep(interval)
    finally:
        if delivery is not None:
            delivery.cancel()
            try:
                await delivery
            except asyncio.CancelledError:
                pass
