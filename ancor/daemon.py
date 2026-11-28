"""Standalone ANCOR scheduling daemon (zero Hermes Gateway dependency).

Runs:
  - GoalReconciler (crash-safe startup recovery)
  - GoalWatcher (zero-LLM polling of external handles, deadlines, and goal_waits)
  - GoalRunner (pluggable ExecutionBackend per goal + category routing)
  - Judge dispatcher (independent clean-context delivery verifier)
  - Root Agent Coordinator (event-driven request orchestrator)
  - MultiNotificationDispatcher (fan-out to all configured notifiers)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .backends import BackendRegistry, BackendRouter, LaunchSpec
from .config import AncorConfig, load_config
from .goals import GoalStore, is_pid_alive
from .host import STALLED_COOLDOWN_SECONDS, find_stalled_active_requests, goal_summary
from .judge import JudgeGate
from .memory import MemoryStore
from .notification import NotificationOutbox
from .notifiers import MultiNotificationDispatcher, build_notifiers
from .runtime import Coordinator
from .store import Store
from .views import (
    actionable_notifications,
    collection,
    completion_id,
    notification_counts,
    notification_selection,
    notification_summary,
    request_ref,
)
from .watcher import FileClusterBackend, GoalReconciler, GoalRunner, GoalWatcher, SessionRotationPolicy

log = logging.getLogger(__name__)


def _default_mcp_command(config: AncorConfig, role: str, goal_id: str | None = None) -> list[str]:
    cmd = [sys.executable, '-m', 'ancor.cli.main']
    if config.config_path is not None:
        cmd.extend(['--config', str(config.config_path)])
    else:
        cmd.extend(['--db', str(config.store_path)])
    cmd.extend(['mcp', '--role', role])
    if goal_id:
        cmd.extend(['--goal-id', goal_id])
    return cmd


def _load_exec_system_prompt() -> str:
    candidate = Path(__file__).resolve().parent.parent / 'roles' / 'ancor-exec' / 'SOUL.md'
    if candidate.exists():
        try:
            return candidate.read_text(encoding='utf-8')
        except OSError:
            pass
    return (
        'You are an ANCOR headless execution agent. Stay strictly within your assigned ANCOR_ROLE '
        '(root, goal, or judge) and use the provided ancor_* MCP tools.'
    )


class AncorDaemon:
    """Standalone deterministic scheduling loop + multi-backend launcher + multi-notifier."""

    def __init__(
        self,
        config: AncorConfig | None = None,
        *,
        store: Store | None = None,
        cluster_backend=None,
        registry: BackendRegistry | None = None,
        clock=time.time,
    ):
        self.config = config or load_config()
        self.clock = clock
        self.store = store or Store(self.config.store_path)
        self.cluster = cluster_backend or FileClusterBackend(self.config.cluster_state_path, clock=clock)
        self.registry = registry or BackendRegistry.from_config(self.config)
        self.router = BackendRouter(self.config, self.registry)
        self.goals = GoalStore(
            self.store,
            clock=clock,
            global_limits=self.config.global_limits or None,
            category_router=self.router,
        )
        self.watcher = GoalWatcher(self.goals, self.cluster, clock=clock)
        self.reconciler = GoalReconciler(self.goals, self.cluster, clock=clock)
        self.memory = MemoryStore(
            self.config.effective_memory_dir(),
            stale_days=self.config.memory_stale_days,
        )
        self.system_prompt = _load_exec_system_prompt()
        self.runner = GoalRunner(
            self.goals,
            backend_router=self.router,
            mcp_command_builder=lambda role, gid=None: _default_mcp_command(self.config, role, gid),
            system_prompt=self._role_system_prompt('goal'),
            rotation_policy=SessionRotationPolicy(
                session_reuse_ttl_seconds=self.config.session_reuse_ttl_seconds,
                max_wakes_per_session=self.config.max_wakes_per_session,
                max_shifts_per_goal=self.config.max_shifts_per_goal,
            ),
        )
        self.judge = JudgeGate(
            self.store,
            self.goals,
            max_rejects=self.config.max_judge_rejects,
            clock=clock,
        )
        self._scan_lock = threading.Lock()
        self.state_path = self.config.store_path.parent / (self.config.store_path.name + '.daemon.json')
        self.startup = True
        self.last_scan_at: float | None = None
        self.last_reconcile: dict | None = None
        self.last_watch: dict | None = None
        self.last_stalled: list[str] = []
        self._running = False

        self.coordinator = Coordinator(
            self.config.store_path.parent / 'ancor-root.lock',
            pending=self.pending,
            snapshot=self.wake_packet,
            scan=self.scan,
            batch_size=self.config.event_batch_size,
            retry_base=15,
            command=self._root_command,
        )

    def _role_system_prompt(self, role: str) -> str:
        mem_ctx = self.memory.build_prompt_context(role=role)
        if not mem_ctx:
            return self.system_prompt
        return f'{self.system_prompt.rstrip()}\n\n{mem_ctx}'

    def _root_command(self, prompt: str) -> list[str]:
        route = self.router.resolve(role='root')
        spec = LaunchSpec(
            role='root',
            prompt=prompt,
            system_prompt=self._role_system_prompt('root'),
            extra_args=route.args,
            mcp_command=_default_mcp_command(self.config, 'root'),
            env={'ANCOR_STORE_PATH': str(self.config.store_path)},
        )
        cmd, env, _tmp = route.backend.build_command(spec)
        # Coordinator runs subprocess with inherited env; prefix env overrides cleanly.
        env_prefix = ['env']
        for key in ('ANCOR_ROLE', 'ANCOR_STORE_PATH', 'HERMES_EPHEMERAL_SYSTEM_PROMPT'):
            if key in env:
                env_prefix.append(f'{key}={env[key]}')
        env_prefix.extend(['-u', 'GOAL_ID', '-u', 'ANCOR_GOAL_ID'])
        return [*env_prefix, *cmd]

    async def _launch_judge_process(self, packet: dict) -> int:
        prompt = (
            'You are the independent ANCOR delivery Judge. Review this delivery packet against the '
            'authoritative requirement (read it with ancor_request_read), then call ancor_judge_verdict '
            'with the packet delivery_event_id and accept, reject (with structured feedback), or escalate.\n'
            + json.dumps(packet, ensure_ascii=False, separators=(',', ':'))
        )
        route = self.router.resolve(role='judge')
        spec = LaunchSpec(
            role='judge',
            prompt=prompt,
            system_prompt=self._role_system_prompt('judge'),
            extra_args=route.args,
            mcp_command=_default_mcp_command(self.config, 'judge'),
            env={'ANCOR_STORE_PATH': str(self.config.store_path)},
        )
        cmd, env, temp_files = route.backend.build_command(spec)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            rc = await proc.wait()
            if rc:
                self.router.mark_backend_cooldown(route.backend.name)
            return rc
        except asyncio.CancelledError:
            await route.backend.stop_session(proc.pid)
            raise
        finally:
            for p in temp_files:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.state_path)

    def scan(self) -> None:
        with self._scan_lock:
            self._scan()

    def _scan(self) -> None:
        self.last_reconcile = self.reconciler.reconcile(managed_goal_ids=tuple(self.runner.tasks))
        self.goals.settle_stops(job_canceller=lambda h: self.cluster.cancel_handle(h['handle_id'], h.get('tags')))
        self.last_watch = self.watcher.poll()
        snapshot = self.store.snapshot()
        self.close_stale_notifications(snapshot)
        self.last_stalled = self._reconcile_stalled_requests(snapshot)
        self.last_scan_at = float(self.clock())
        self._write_status()

    def _reconcile_stalled_requests(self, snapshot: dict) -> list[str]:
        stalled = find_stalled_active_requests(self.store, self.goals, snapshot=snapshot)
        state = self._load_state()
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
            self._save_state(state)
        return emitted

    def notification_outbox(self) -> NotificationOutbox:
        return NotificationOutbox(
            self.config.notifications_path,
            coordinator_profile='root',
            target='ancor:outbox',
            clock=self.clock,
        )

    def close_stale_notifications(self, snapshot: dict) -> list[dict]:
        path = self.config.notifications_path
        if not path.exists() and not self.store.questions():
            return []
        requests = {row['id']: row for row in snapshot['requests']}
        box = self.notification_outbox()
        self.store.sync_questions(box)
        decisions = {}
        for row in box.list():
            if row['kind'] != 'question' or row['answer'] is not None or row['closed_at'] is not None:
                continue
            req = requests.get(row.get('request_id'))
            if req is None:
                decisions[row['id']] = 'request_missing'
            elif req.get('target_status') in {'cancelled', 'invalidated'}:
                decisions[row['id']] = 'request_' + req['target_status']
            elif req.get('target_status') is None and req['status'] in {'cancelled', 'invalidated', 'accepted'}:
                decisions[row['id']] = 'request_' + req['status']
        if decisions:
            box.close_questions(decisions, closed_by='host')
        for row in box.list():
            if row['kind'] != 'completion' or row.get('closed_at') is not None:
                continue
            req = requests.get(row.get('request_id'))
            if req is None:
                box.close_delivery(row['id'], closed_by='host', reason='request_missing')
            elif req.get('content_mode') == 'points' and row['id'] != completion_id(req):
                box.close_delivery(row['id'], closed_by='host', reason='verification_superseded')
            elif req.get('status') != 'awaiting_acceptance' or req.get('target_status'):
                box.close_completions(req['id'], closed_by='host', reason='completion_no_longer_pending')
        return box.list()

    def pending(self, limit: int) -> list[dict]:
        if self.startup:
            self.startup = False
        pending = self.store.pending_events()
        for work in self.store.pending_coordination():
            key = f"coordination:{work['id']}:{work['generation']}"
            if not any(e.get('source_key') == key for e in pending):
                event = self.store.add_event(
                    'request.generation_changed',
                    {
                        'request_id': work['id'],
                        'generation': work['generation'],
                        'observed_generation': work['observed_generation'],
                    },
                    source_key=key,
                )
                if event['acknowledged']:
                    with self.store._transaction() as db:
                        db.execute('UPDATE events SET acknowledged=0 WHERE id=?', (event['id'],))
        pending = [e for e in self.store.pending_events() if e['kind'] != 'delivery.submitted']
        state = self._load_state()
        now = float(self.clock())
        retries = state.setdefault('event_retries', {})
        pending_ids = {str(e['id']) for e in pending}
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
                continue
            eligible.append(event)
        events = eligible[:limit] if len(eligible) <= limit else eligible[:limit - 1] + eligible[-1:]
        for event in events:
            retries[str(event['id'])]['attempts'] += 1
        self._save_state(state)
        return events

    def wake_packet(self) -> dict:
        data = self.store.snapshot()
        notifications = NotificationOutbox.read_existing(self.config.notifications_path)
        delivered = {n['id'] for n in notifications if n['state'] == 'sent' and n['kind'] == 'completion'}
        pending_completion = {
            n['request_id'] for n in notifications
            if n['kind'] == 'completion' and n.get('closed_at') is None and n.get('request_id')
        }
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
            'model_categories': sorted(self.router.alias_names()),
            'questions': collection([
                {'id': q['id'], 'request_id': q['request_id'], 'revision': q['revision'], 'state': q['state']}
                for q in self.store.questions() if q['state'] == 'open'
            ]),
            'notifications': collection(
                [notification_summary(row) for row in selected], total=len(visible_notifications)),
            'notification_counts': notification_counts(notifications),
            'expand': 'Call ancor_request_get for request detail, or ancor_goal_get for one goal.',
        }

    async def _renew_judge_claim(self, event_id: int) -> None:
        while True:
            await asyncio.sleep(20)
            with self.store._transaction() as db:
                db.execute(
                    'UPDATE judge_deliveries SET retry_at=? WHERE event_id=? AND result_json IS NULL',
                    (float(self.clock()) + 60, event_id),
                )

    async def dispatch_judges(self) -> list[int]:
        events = [e for e in self.store.pending_events() if e['kind'] == 'delivery.submitted']
        launched: list[int] = []
        for event in events:
            eid = event['id']
            now = float(self.clock())
            with self.store._transaction() as db:
                db.execute('INSERT OR IGNORE INTO judge_deliveries(event_id) VALUES (?)', (eid,))
                state = db.execute('SELECT * FROM judge_deliveries WHERE event_id=?', (eid,)).fetchone()
                if not state['result_json'] and state['attempts'] >= 3 and state['retry_at'] <= now:
                    self.store._add_event(
                        db, 'judge.failed',
                        {
                            'delivery_event_id': eid,
                            'request_id': event['payload'].get('request_id'),
                            'reason': state['error'] or 'Judge claim expired without verdict',
                            'attempts': state['attempts'],
                        },
                        source_key=f'judge.failed:{eid}',
                    )
                if state['result_json'] or state['attempts'] >= 3 or state['retry_at'] > now:
                    continue
                db.execute(
                    'UPDATE judge_deliveries SET attempts=attempts+1, retry_at=? WHERE event_id=?',
                    (now + 60, eid),
                )
            renewal = asyncio.create_task(self._renew_judge_claim(eid))
            error = 'Judge exited without recording a verdict'
            try:
                packet = (event.get('payload') or {}).get('judge_packet')
                if not packet:
                    raise ValueError('delivery has no judge_packet')
                launched.append(eid)
                rc = await self._launch_judge_process({**packet, 'delivery_event_id': eid})
                if rc:
                    error = f'Judge exited with code {rc} without recording a verdict'
            except asyncio.CancelledError:
                error = 'Daemon interrupted Judge execution'
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
                        db.execute(
                            'UPDATE judge_deliveries SET retry_at=?, error=? WHERE event_id=?',
                            (float(self.clock()) + 5 * 2 ** state['attempts'], error, eid),
                        )
                        if state['attempts'] >= 3:
                            self.store._add_event(
                                db, 'judge.failed',
                                {
                                    'delivery_event_id': eid,
                                    'request_id': event['payload'].get('request_id'),
                                    'reason': error,
                                    'attempts': state['attempts'],
                                },
                                source_key=f'judge.failed:{eid}',
                            )
        return launched

    def _write_status(self) -> None:
        status_path = self.config.effective_daemon_state_path()
        status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'pid': os.getpid(),
            'store_path': str(self.config.store_path),
            'config_path': str(self.config.config_path) if self.config.config_path else None,
            'last_scan_at': self.last_scan_at,
            'active_goal_tasks': sorted(self.runner.tasks.keys()),
            'backends': self.registry.names(),
            'notifiers': [n.name for n in self.config.notifiers],
        }
        tmp = status_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(status_path)

    async def run_once(self) -> None:
        await asyncio.to_thread(self.scan)
        await self.runner.dispatch_ready()
        await self.dispatch_judges()
        await self.coordinator.tick()

    async def run_forever(self) -> None:
        pid_path = self.config.effective_daemon_pid_path()
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()), encoding='utf-8')
        self._running = True

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _request_stop():
            self._running = False
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:
                pass

        sinks = build_notifiers(self.config.notifiers)
        delivery_task: asyncio.Task | None = None
        if sinks:
            dispatcher = MultiNotificationDispatcher(
                self.notification_outbox(),
                sinks,
                resolve_request=self.store.get_request,
                retry_seconds=max(10.0, self.config.poll_interval),
                clock=self.clock,
            )
            delivery_task = asyncio.create_task(dispatcher.run(lambda: self._running), name='ancor:notifiers')

        control_tasks: dict[str, asyncio.Task] = {}
        try:
            while self._running:
                await asyncio.to_thread(self.scan)
                await self.runner.dispatch_ready()
                for name, op in (('judges', self.dispatch_judges), ('root', self.coordinator.tick)):
                    task = control_tasks.get(name)
                    if task is not None and task.done():
                        try:
                            task.result()
                        except Exception:
                            log.exception('ANCOR daemon %s task failed', name)
                        task = None
                    if task is None:
                        control_tasks[name] = asyncio.create_task(op(), name=f'ancor:{name}')
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.config.poll_interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            self._running = False
            for task in control_tasks.values():
                task.cancel()
            await asyncio.gather(*control_tasks.values(), return_exceptions=True)
            await self.runner.close()
            if delivery_task is not None:
                delivery_task.cancel()
                await asyncio.gather(delivery_task, return_exceptions=True)
            try:
                if pid_path.exists() and pid_path.read_text(encoding='utf-8').strip() == str(os.getpid()):
                    pid_path.unlink(missing_ok=True)
            except OSError:
                pass


def daemon_status(config: AncorConfig) -> dict:
    pid_path = config.effective_daemon_pid_path()
    state_path = config.effective_daemon_state_path()
    pid = None
    running = False
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding='utf-8').strip())
            running = is_pid_alive(pid)
        except (ValueError, OSError):
            pid = None
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            state = {}
    return {
        'running': running,
        'pid': pid if running else None,
        'pid_path': str(pid_path),
        'store_path': str(config.store_path),
        'config_path': str(config.config_path) if config.config_path else None,
        **({k: v for k, v in state.items() if k not in {'pid', 'store_path', 'config_path'}}),
    }


def start_daemon_background(config: AncorConfig) -> dict:
    st = daemon_status(config)
    if st['running']:
        return {'started': False, 'already_running': True, **st}
    log_path = config.runtime_dir / 'ancor-daemon.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, '-m', 'ancor.cli.main', 'daemon', 'run']
    if config.config_path is not None:
        cmd.extend(['--config', str(config.config_path)])
    else:
        cmd.extend(['--db', str(config.store_path)])
    with log_path.open('a', encoding='utf-8') as log_file:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    return {'started': True, 'pid': proc.pid, 'log_path': str(log_path), 'store_path': str(config.store_path)}


def stop_daemon_background(config: AncorConfig, *, timeout: float = 10.0) -> dict:
    st = daemon_status(config)
    if not st['running'] or not st['pid']:
        return {'stopped': False, 'was_running': False}
    pid = st['pid']
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {'stopped': True, 'was_running': True, 'pid': pid}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return {'stopped': True, 'was_running': True, 'pid': pid}
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return {'stopped': True, 'was_running': True, 'pid': pid, 'forced': True}
