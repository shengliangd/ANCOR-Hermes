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
import signal
import threading
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
STALLED_COOLDOWN_SECONDS = 900


def find_stalled_active_requests(store, goal_store, snapshot=None) -> list[dict]:
    """Return stably active requests with 0 active goals and 0 pending events that need Root attention.

    Includes:
    - Requests with 0 open questions (periodic watchdog).
    - Requests with open question(s) where the latest goal updated *after* the latest open question
      was created (early/non-blocking question whose parallel goal later stopped; checked once).
    """
    snapshot = snapshot or store.snapshot()
    active_reqs = [
        r for r in snapshot['requests']
        if r['status'] == 'active' and r.get('target_status') is None
    ]
    if not active_reqs:
        return []
    active_goal_reqs = {
        g['request_id'] for g in goal_store.list_goals(include_terminal=False)
    }
    latest_goal_updated: dict[str, float] = {}
    for g in goal_store.list_goals(include_terminal=True):
        rid = g['request_id']
        ts = float(g.get('updated_at') or g.get('created_at') or 0.0)
        if ts > latest_goal_updated.get(rid, 0.0):
            latest_goal_updated[rid] = ts

    latest_open_q_created: dict[str, float] = {}
    for q in store.questions():
        if q.get('state') != 'open':
            continue
        rid = q['request_id']
        ts = float(q.get('created_at') or 0.0)
        if ts >= latest_open_q_created.get(rid, -1.0):
            latest_open_q_created[rid] = ts

    pending_event_reqs = {
        (e.get('payload') or {}).get('request_id')
        for e in store.pending_events()
        if isinstance(e.get('payload'), dict) and (e.get('payload') or {}).get('request_id')
    }
    stalled = []
    for req in active_reqs:
        rid = req['id']
        if rid in active_goal_reqs or rid in pending_event_reqs:
            continue
        has_open_q = rid in latest_open_q_created
        g_ts = latest_goal_updated.get(rid, 0.0)
        if has_open_q and g_ts <= latest_open_q_created[rid]:
            continue
        stalled.append({
            'request_id': rid,
            'title': req.get('title', ''),
            'generation': req['generation'],
            'has_open_questions': has_open_q,
            'latest_goal_updated_at': g_ts,
        })
    return stalled


def child_environment(environ=None):
    """Drop routing identity only; retain ALL native delegation fences."""
    source = dict(os.environ if environ is None else environ)
    return {k: v for k, v in source.items()
            if not k.startswith('HERMES_SESSION_') and k not in {
                'HERMES_UI_SESSION_ID', 'HERMES_BROWSER_CONTROL_PRINCIPAL',
                'HERMES_BROWSER_CONTROL_TRANSPORT_FAMILY', 'HERMES_CRON_SESSION'}}


def _exec_role_command(role: str, exec_profile: str, prompt: str, model_args=()):
    from .session_identity import launch_identity
    clean = child_environment()
    removed = sorted((set(os.environ) - set(clean)) | {'GOAL_ID', 'ANCOR_GOAL_ID'})
    return ['env', *[part for key in removed for part in ('-u', key)],
            f'ANCOR_ROLE={role}',
            f'HERMES_EPHEMERAL_SYSTEM_PROMPT={launch_identity(role)}',
            'hermes', '-p', exec_profile, '--cli', 'chat', '--yolo', *model_args, '-q', prompt]


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
        self._scan_lock = threading.Lock()
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
        with self._scan_lock:
            self._scan()

    def _scan(self):
        """Deterministic scan: crash-safe startup reconcile + zero-LLM Watcher + stale notices + liveness."""
        self.last_reconcile = self.reconciler.reconcile(managed_goal_ids=tuple(self.runner.tasks))
        self.goals.settle_stops(job_canceller=lambda h: self.cluster.cancel_handle(h['handle_id'], h.get('tags')))
        self.last_watch = self.watcher.poll()
        snapshot = self.store.snapshot()
        self.close_stale_notifications(snapshot)
        self.last_stalled = self._reconcile_stalled_requests(snapshot)

    def _reconcile_stalled_requests(self, snapshot=None) -> list[str]:
        """Emit request.stalled for dropped active requests (periodic without open questions, one-shot with earlier open questions)."""
        snapshot = snapshot or self.store.snapshot()
        stalled = find_stalled_active_requests(self.store, self.goals, snapshot=snapshot)
        state = self.state()
        cooldowns = state.setdefault('stalled_last_emitted', {})
        seen_open_q = state.setdefault('stalled_open_q_seen', {})
        stalled_no_q_ids = {item['request_id'] for item in stalled if not item.get('has_open_questions')}
        active_req_ids = {r['id'] for r in snapshot['requests'] if r['status'] == 'active'}
        dirty = False
        for rid in list(cooldowns):
            if rid not in stalled_no_q_ids:
                del cooldowns[rid]
                dirty = True
        for rid in list(seen_open_q):
            if rid not in active_req_ids:
                del seen_open_q[rid]
                dirty = True
        now = float(self.clock())
        emitted: list[str] = []
        for item in stalled:
            rid = item['request_id']
            if item.get('has_open_questions'):
                stamp = f"gen{item['generation']}:{item['latest_goal_updated_at']:.6f}"
                if seen_open_q.get(rid) == stamp:
                    continue
                self.store.add_event(
                    'request.stalled',
                    {
                        'request_id': rid,
                        'generation': item['generation'],
                        'reason': 'active_request_goals_stopped_with_earlier_open_question',
                        'instruction': (
                            'Active request has 0 active goals after a goal finished/blocked later than '
                            'its open question(s). Verify whether parallel work can continue; if truly '
                            'blocked on the open question(s), ack without spawning.'
                        ),
                    },
                    source_key=f'stalled_openq:{rid}:gen{item["generation"]}:{int(item["latest_goal_updated_at"] * 1000)}',
                )
                seen_open_q[rid] = stamp
                dirty = True
                emitted.append(rid)
                continue

            last_at = float(cooldowns.get(rid, 0.0) or 0.0)
            if last_at and (now - last_at) < STALLED_COOLDOWN_SECONDS:
                continue
            bucket = int(now // STALLED_COOLDOWN_SECONDS)
            self.store.add_event(
                'request.stalled',
                {
                    'request_id': rid,
                    'generation': item['generation'],
                    'reason': 'active_request_without_goals_or_open_questions',
                    'instruction': 'Active request has 0 active goals and 0 open questions; spawn next /goal, ask a question, or submit delivery.',
                },
                source_key=f'stalled:{rid}:gen{item["generation"]}:b{bucket}',
            )
            cooldowns[rid] = now
            dirty = True
            emitted.append(rid)
        if dirty:
            self.save(state)
        return emitted

    def judge_command_for(self, packet: dict) -> list[str]:
        prompt = (
            'You are the independent ANCOR delivery Judge. Review this delivery packet against the '
            'authoritative requirement (read it with ancor_request_read), then call ancor_judge_verdict '
            'with the packet delivery_event_id and accept, reject (with structured feedback), or escalate.\n'
            + json.dumps(packet, ensure_ascii=False, separators=(',', ':')))
        return judge_command(self.exec_profile, prompt,
                             self.router.cli_args(self.router.role_category('judge')))

    async def _launch_judge_process(self, packet: dict) -> int:
        proc = await asyncio.create_subprocess_exec(
            *self.judge_command_for(packet),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            return await proc.wait()
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            raise

    async def _renew_judge_claim(self, event_id):
        while True:
            await asyncio.sleep(20)
            with self.store._transaction() as db:
                db.execute('UPDATE judge_deliveries SET retry_at=? WHERE event_id=? AND result_json IS NULL',
                           (float(self.clock()) + 60, event_id))

    async def dispatch_judges(self) -> list[int]:
        """Claim durable deliveries; only a committed verdict acknowledges them."""
        events = [e for e in self.store.pending_events() if e['kind'] == 'delivery.submitted']
        launched = []
        for event in events:
            eid = event['id']
            now = float(self.clock())
            with self.store._transaction() as db:
                db.execute('INSERT OR IGNORE INTO judge_deliveries(event_id) VALUES (?)', (eid,))
                state = db.execute('SELECT * FROM judge_deliveries WHERE event_id=?', (eid,)).fetchone()
                if not state['result_json'] and state['attempts'] >= 3 and state['retry_at'] <= now:
                    self.store._add_event(db, 'judge.failed',
                        {'delivery_event_id': eid, 'request_id': event['payload'].get('request_id'),
                         'reason': state['error'] or 'Judge claim expired without verdict', 'attempts': state['attempts']},
                        source_key=f'judge.failed:{eid}')
                if state['result_json'] or state['attempts'] >= 3 or state['retry_at'] > now:
                    continue
                db.execute('UPDATE judge_deliveries SET attempts=attempts+1, retry_at=? WHERE event_id=?',
                           (now + 60, eid))
            renewal = asyncio.create_task(self._renew_judge_claim(eid))
            error = 'Judge exited without recording a verdict'
            try:
                packet = (event.get('payload') or {}).get('judge_packet')
                if not packet:
                    raise ValueError('delivery has no judge_packet')
                launched.append(eid)
                rc = await self.judge_launcher({**packet, 'delivery_event_id': eid})
                if rc:
                    error = f'Judge exited with code {rc} without recording a verdict'
            except asyncio.CancelledError:
                error = 'Host interrupted Judge execution'
                raise
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
                log.exception('ANCOR Judge failed for event %s', eid)
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
                with self.store._transaction() as db:
                    state = db.execute('SELECT * FROM judge_deliveries WHERE event_id=?', (eid,)).fetchone()
                    if not state['result_json']:
                        db.execute('UPDATE judge_deliveries SET retry_at=?, error=? WHERE event_id=?',
                                   (float(self.clock()) + 5 * 2 ** state['attempts'], error, eid))
                        if state['attempts'] >= 3:
                            self.store._add_event(db, 'judge.failed',
                                {'delivery_event_id': eid, 'request_id': event['payload'].get('request_id'),
                                 'reason': error, 'attempts': state['attempts']},
                                source_key=f'judge.failed:{eid}')
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
    control_tasks = {}
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
            for name, operation in (('judges', host.dispatch_judges), ('root', host.coordinator.tick)):
                task = control_tasks.get(name)
                if task is not None and task.done():
                    try:
                        outcome = task.result()
                        if outcome == 'failed':
                            log.error('ANCOR %s infrastructure failure: %s', name, host.coordinator.last_error)
                    except Exception:
                        log.exception('ANCOR %s task failed', name)
                    task = None
                if task is None:
                    control_tasks[name] = asyncio.create_task(operation(), name=f'ancor:{name}')
            await asyncio.sleep(interval)
    finally:
        for task in control_tasks.values():
            task.cancel()
        await asyncio.gather(*control_tasks.values(), return_exceptions=True)
        await host.runner.close()
        if delivery is not None:
            delivery.cancel()
            try:
                await delivery
            except asyncio.CancelledError:
                pass
