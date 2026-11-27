"""Deterministic Watcher (zero LLM cost), Crash-Safe Reconciler, and Goal Runner.

Replaces the legacy ``wake.due`` LLM polling loop and Hermes built-in
``delegate_task`` with:
  - FileClusterBackend / pluggable ClusterBackend for external jobs/resources
  - GoalWatcher: polls external handles & wall-clock deadlines with zero LLM cost;
    wakes a goal session only on real state transitions (and never while paused)
  - GoalReconciler: crash-safe startup recovery for active_goals
  - GoalRunner: launches independent persistent Hermes sessions under ancor-exec
    with ANCOR_ROLE=goal and GOAL_ID=<goal_id>
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
import subprocess
import time

from .goals import ACTIVE_HANDLE_STATUSES, GoalStore, is_pid_alive

log = logging.getLogger(__name__)

TERMINAL_HANDLE_STATUSES = frozenset({'succeeded', 'failed', 'cancelled'})


class ClusterBackend:
    """Protocol for submitting, querying, and cancelling external job/resource handles."""

    def submit_handle(self, *, handle_id: str, usage: dict, tags: dict,
                      goal_id: str, command=None, metadata=None) -> dict:
        raise NotImplementedError

    def query_handle(self, handle_id: str, tags: dict | None = None) -> dict:
        raise NotImplementedError

    def cancel_handle(self, handle_id: str, tags: dict | None = None) -> bool:
        raise NotImplementedError

    def summary(self) -> dict:
        return {}


class FileClusterBackend(ClusterBackend):
    """Durable JSON-backed external resource/job backend for tests and isolated trials."""

    def __init__(self, path: str | Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    def _load(self) -> dict:
        if not self.path.exists():
            return {'handles': {}}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) and isinstance(data.get('handles'), dict) else {'handles': {}}
        except (OSError, ValueError):
            return {'handles': {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def submit_handle(self, *, handle_id: str, usage: dict, tags: dict,
                      goal_id: str, command=None, metadata=None) -> dict:
        data = self._load()
        entry = {
            'handle_id': handle_id,
            'usage': dict(usage),
            'tags': dict(tags),
            'goal_id': goal_id,
            'status': 'running',
            'command': command,
            'metadata': metadata or {},
            'submitted_at': float(self.clock()),
            'updated_at': float(self.clock()),
            'detail': None,
        }
        data['handles'][handle_id] = entry
        self._save(data)
        return dict(entry)

    def set_status(self, handle_id: str, status: str, detail: str | None = None) -> dict:
        data = self._load()
        if handle_id not in data['handles']:
            raise KeyError(handle_id)
        data['handles'][handle_id]['status'] = status
        data['handles'][handle_id]['detail'] = detail
        data['handles'][handle_id]['updated_at'] = float(self.clock())
        self._save(data)
        return dict(data['handles'][handle_id])

    def query_handle(self, handle_id: str, tags: dict | None = None) -> dict:
        data = self._load()
        handle = data['handles'].get(handle_id)
        if handle is None:
            return {'handle_id': handle_id, 'status': 'unknown', 'detail': 'not_found'}
        return dict(handle)

    def cancel_handle(self, handle_id: str, tags: dict | None = None) -> bool:
        data = self._load()
        handle = data['handles'].get(handle_id)
        if handle is None:
            return False
        if handle['status'] in ACTIVE_HANDLE_STATUSES:
            handle['status'] = 'cancelled'
            handle['detail'] = 'cancelled_by_ancor'
            handle['updated_at'] = float(self.clock())
            self._save(data)
            return True
        return handle['status'] in TERMINAL_HANDLE_STATUSES

    def summary(self) -> dict:
        data = self._load()
        active_usage: dict[str, float] = {}
        active_count = 0
        for handle in data['handles'].values():
            if handle.get('status') in ACTIVE_HANDLE_STATUSES:
                active_count += 1
                for k, v in (handle.get('usage') or {}).items():
                    active_usage[k] = active_usage.get(k, 0.0) + float(v)
        return {
            'active_handles': active_count,
            'active_usage': active_usage,
            'total_handles': len(data['handles']),
        }


class GoalWatcher:
    """Zero-LLM deterministic watcher for external handles and wall-clock deadlines."""

    def __init__(self, goal_store: GoalStore, cluster_backend: ClusterBackend | None = None,
                 *, clock=time.time):
        self.goal_store = goal_store
        self.cluster_backend = cluster_backend
        self.clock = clock

    def poll(self) -> dict:
        now = float(self.clock())
        transitions: list[dict] = []
        timeouts: list[dict] = []
        woken_goals: list[str] = []

        goals = self.goal_store.list_goals(include_terminal=False)
        for goal in goals:
            gid = goal['goal_id']
            changed = False
            wake_this = False
            handles = list(goal['external_job_ids'])
            signals = []

            if self.cluster_backend is not None and handles:
                updated_handles = []
                for handle in handles:
                    hid = handle['handle_id']
                    if handle.get('status', 'running') in ACTIVE_HANDLE_STATUSES:
                        info = self.cluster_backend.query_handle(hid, handle.get('tags'))
                        new_status = info.get('status', handle.get('status'))
                        if new_status in TERMINAL_HANDLE_STATUSES and new_status != handle.get('status'):
                            changed = True
                            updated_handle = {
                                **handle,
                                'status': new_status,
                                'detail': info.get('detail'),
                                'finished_at': now,
                            }
                            updated_handles.append(updated_handle)
                            transition = {
                                'event': 'handle_transition',
                                'goal_id': gid,
                                'handle_id': hid,
                                'status': new_status,
                                'detail': info.get('detail'),
                            }
                            transitions.append(transition)
                            signals.append({'kind': 'watcher_event', 'payload': transition, 'at': now})
                            if goal['status'] != 'paused':
                                wake_this = True
                            continue
                    updated_handles.append(handle)
                handles = updated_handles

            deadline = goal.get('deadline_at')
            if (goal['status'] != 'paused'
                    and deadline is not None
                    and now >= float(deadline)
                    and not any(
                        s.get('kind') == 'watcher_event'
                        and (s.get('payload') or {}).get('event') == 'wall_clock_timeout'
                        for s in [*goal['pending_signals'], *signals]
                    )):
                changed = True
                wake_this = True
                timeout_payload = {
                    'event': 'wall_clock_timeout',
                    'goal_id': gid,
                    'deadline_at': deadline,
                }
                timeouts.append(timeout_payload)
                signals.append({'kind': 'watcher_event', 'payload': timeout_payload, 'at': now})

            if changed:
                with self.goal_store.store._transaction() as db:
                    current = self.goal_store._get(db, gid)
                    if current['status'] not in {'running', 'waiting_external', 'paused'}:
                        continue
                    patches = {h['handle_id']: h for h in handles
                               if h.get('status') in TERMINAL_HANDLE_STATUSES}
                    handles = [patches.get(h['handle_id'], h)
                               if h.get('status', 'running') in ACTIVE_HANDLE_STATUSES else h
                               for h in current['external_job_ids']]
                    signals = [*current['pending_signals'], *signals]
                    new_status = current['status']
                    new_wake = current['needs_wake']
                    if wake_this and current['status'] != 'paused':
                        new_wake = True
                        if current['status'] == 'waiting_external':
                            new_status = 'running'
                    db.execute(
                        'UPDATE active_goals SET external_job_ids=?, pending_signals=?, '
                        'status=?, needs_wake=?, updated_at=? WHERE goal_id=?',
                        (
                            json.dumps(handles, ensure_ascii=False),
                            json.dumps(signals, ensure_ascii=False),
                            new_status,
                            int(new_wake),
                            now,
                            gid,
                        ))
                if wake_this and goal['status'] != 'paused':
                    woken_goals.append(gid)

        return {
            'transitions': transitions,
            'timeouts': timeouts,
            'woken_goals': woken_goals,
        }


class GoalReconciler:
    """Crash-safe startup reconciler for active_goals after Gateway/host restart."""

    def __init__(self, goal_store: GoalStore, cluster_backend: ClusterBackend | None = None,
                 *, clock=time.time, pid_alive=is_pid_alive):
        self.goal_store = goal_store
        self.cluster_backend = cluster_backend
        self.clock = clock
        self.pid_alive = pid_alive

    def reconcile(self, *, managed_goal_ids=()) -> dict:
        now = float(self.clock())
        waiting_external: list[str] = []
        recovered_running: list[str] = []
        still_alive: list[str] = []

        goals = self.goal_store.list_goals(include_terminal=False)
        for goal in goals:
            gid = goal['goal_id']
            if gid in managed_goal_ids:
                continue
            if goal['status'] == 'paused':
                continue
            if goal['status'] == 'waiting_external':
                waiting_external.append(gid)
                continue
            pid = goal.get('pid')
            if pid and self.pid_alive(pid):
                still_alive.append(gid)
                continue
            if not pid and goal['needs_wake']:
                continue

            live_handles = []
            for handle in goal['external_job_ids']:
                hid = handle['handle_id']
                if self.cluster_backend is not None:
                    info = self.cluster_backend.query_handle(hid, handle.get('tags'))
                    live_handles.append({
                        'handle_id': hid,
                        'usage': handle.get('usage', {}),
                        'tags': handle.get('tags', {}),
                        'status': info.get('status', handle.get('status')),
                        'detail': info.get('detail'),
                    })
                else:
                    live_handles.append({
                        'handle_id': hid,
                        'usage': handle.get('usage', {}),
                        'tags': handle.get('tags', {}),
                        'status': handle.get('status', 'unknown'),
                    })

            recovery_signal = {
                'kind': 'recovery',
                'payload': {
                    'reason': 'host_restarted_while_running',
                    'external_handles': live_handles,
                    'instruction': (
                        'Host restarted while this goal was running. Inspect workspace and '
                        'registered external_handles first; strictly do NOT submit duplicate jobs '
                        'for work already running or completed.'
                    ),
                },
                'at': now,
            }
            with self.goal_store.store._transaction() as db:
                current = self.goal_store._get(db, gid)
                signals = list(current['pending_signals'])
                signals.append(recovery_signal)
                db.execute(
                    'UPDATE active_goals SET pid=NULL, needs_wake=1, pending_signals=?, '
                    'updated_at=? WHERE goal_id=?',
                    (json.dumps(signals, ensure_ascii=False), now, gid))
            recovered_running.append(gid)

        return {
            'waiting_external': waiting_external,
            'recovered_running': recovered_running,
            'still_alive': still_alive,
        }


def build_goal_prompt(goal: dict, signals: list[dict], *, model_categories=None) -> str:
    """Build the structured prompt passed to an independent persistent goal session."""
    packet = {
        'goal_id': goal['goal_id'],
        'session_title': f"ancor-{goal['request_id']}-{goal['goal_id']}",
        'parent_goal_id': goal['parent_goal_id'],
        'request_ref': goal['request_ref'],
        'objective': goal['objective'],
        'done_criteria': goal['done_criteria'],
        'scope': goal['scope'],
        'budget': goal['budget'],
        'category': goal.get('category'),
        'model_categories': sorted(model_categories) if model_categories else [],
        'external_handles': goal['external_job_ids'],
        'signals': signals,
    }
    return (
        'Execute or continue this ANCOR /goal within your authorized scope and budget '
        '(limits, allowed, and natural-language constraints). '
        'Read authoritative requirement text directly with ancor_request_read when needed. '
        'Use ancor_resource_acquire for tracked external resources/jobs, ancor_goal_spawn '
        'only when parallel/nested sub-goals are needed, and ancor_goal_report when done/blocked/infeasible.\n'
        + json.dumps(packet, ensure_ascii=False, separators=(',', ':'))
    )


def default_goal_command(profile: str, session_id: str, prompt: str, *, resume: bool,
                         model_args=()) -> list[str]:
    # model_args is ['-m', <category>] (+ ['--reasoning', lvl]); Hermes resolves the alias natively.
    cmd = ['hermes', '-p', profile, '--cli', 'chat', '--format', 'stream-json', *model_args]
    if resume and session_id:
        cmd.extend(['--resume', session_id])
    elif '\n' in prompt:
        packet = json.loads(prompt.split('\n', 1)[1])
        if packet.get('session_title'):
            cmd.extend(['-c', packet['session_title'], '--create-if-missing'])
    cmd.extend(['-q', prompt])
    return cmd


class GoalRunner:
    """Launches independent persistent Hermes processes under ancor-exec with ANCOR_ROLE=goal."""

    def __init__(self, goal_store: GoalStore, *, profile: str = 'ancor-exec',
                 command_builder=None, env_builder=None, category_router=None, native_sessions=None):
        self.goal_store = goal_store
        self.profile = profile
        self.command_builder = command_builder or default_goal_command
        self.env_builder = env_builder
        self.category_router = category_router if category_router is not None else goal_store.category_router
        self.tasks: dict[str, asyncio.Task] = {}
        self.native_sessions = command_builder is None if native_sessions is None else native_sessions

    def _launch(self, goal: dict, signals: list[dict], *, resume: bool) -> list[str]:
        router = self.category_router
        names = router.alias_names() if router is not None else None
        prompt = build_goal_prompt(goal, signals, model_categories=names)
        category = goal.get('category')
        if category and router is not None and (names is None or category not in names):
            # Alias was removed from ancor-exec/config.yaml after spawn: fail loud via a
            # recovery-visible signal rather than silently running on another model.
            raise RuntimeError(
                f"goal {goal['goal_id']} category {category!r} no longer exists in model_aliases "
                'or the exec profile config is unavailable')
        model_args = router.cli_args(category) if router is not None else (['-m', category] if category else [])
        return self.command_builder(self.profile, goal['session_id'], prompt,
                                    resume=resume, model_args=model_args)

    def _child_env(self, goal: dict) -> dict:
        base = dict(self.env_builder() if self.env_builder else os.environ)
        for key in list(base):
            if key.startswith('HERMES_SESSION_') or key in {
                'HERMES_UI_SESSION_ID', 'HERMES_BROWSER_CONTROL_PRINCIPAL',
                'HERMES_BROWSER_CONTROL_TRANSPORT_FAMILY', 'HERMES_CRON_SESSION',
            }:
                base.pop(key, None)
        base['ANCOR_ROLE'] = 'goal'
        base['GOAL_ID'] = goal['goal_id']
        base['ANCOR_GOAL_ID'] = goal['goal_id']
        base['ANCOR_STORE_PATH'] = self.goal_store.store.path
        from .session_identity import launch_identity
        base['HERMES_EPHEMERAL_SYSTEM_PROMPT'] = launch_identity('goal', goal['goal_id'])
        return base

    def run_goal_sync(self, goal_id: str) -> int:
        return asyncio.run(self.run_goal(goal_id))

    async def _read_session_output(self, goal_id, stream, *, stderr=False):
        while line := await stream.readline():
            text = line.decode('utf-8', errors='replace').strip()
            session_id = None
            if stderr:
                # Native CLI emits this as soon as session.create/resume returns,
                # before the first model call, including in stream-json mode.
                if text.startswith('Session: '):
                    session_id = text.removeprefix('Session: ').strip()
            else:
                try:
                    event = json.loads(text)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get('type') in {'system', 'result'}:
                    session_id = event.get('session_id')
            if isinstance(session_id, str) and session_id.strip():
                self.goal_store.bind_session(goal_id, session_id)

    async def run_goal(self, goal_id: str) -> int:
        goal = self.goal_store.get_goal(goal_id)
        if goal['status'] != 'running':
            return 0
        signals = self.goal_store.consume_signals(goal_id)
        proc = None
        spawn = None
        error = None
        interrupted = False
        readers = []
        try:
            resume = bool(goal['session_id'])
            cmd = self._launch(goal, signals, resume=resume)
            if self.native_sessions:
                self.goal_store.prepare_native_session(goal_id, self.profile)
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *cmd, env=self._child_env(goal), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024,
                start_new_session=True))
            proc = await asyncio.shield(spawn)
            registered = self.goal_store.set_process(goal_id, proc.pid, goal['session_id'])
            readers = [asyncio.create_task(self._read_session_output(goal_id, proc.stdout)),
                       asyncio.create_task(self._read_session_output(goal_id, proc.stderr, stderr=True))]
            if registered['status'] != 'running':
                raise asyncio.CancelledError
            rc = await proc.wait()
            await asyncio.gather(*readers)
            if rc:
                error = f'process exited with code {rc}'
            return rc
        except asyncio.CancelledError:
            interrupted = True
            if proc is None and spawn is not None:
                proc = await asyncio.shield(spawn)
                self.goal_store.set_process(goal_id, proc.pid, goal['session_id'])
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await proc.wait()
            raise
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            if proc is None or proc.returncode is not None:
                if self.native_sessions and (interrupted or error):
                    self.goal_store.request_execution_stop(goal_id)
                    await asyncio.to_thread(self.goal_store.settle_stops, goal_ids=[goal_id])
                self.goal_store.finish_run(goal_id, signals, error=error, interrupted=interrupted)

    async def dispatch_ready(self) -> list[str]:
        # Keep ownership until the completed task has been inspected. A second
        # scan can occur before a newly created task has persisted its PID.
        for gid, task in list(self.tasks.items()):
            if task.done():
                del self.tasks[gid]
                if not task.cancelled() and task.exception() is not None:
                    log.error('ANCOR Goal %s failed: %s', gid, task.exception())
        ready = self.goal_store.goals_needing_wake()
        launched = []
        for goal in ready:
            gid = goal['goal_id']
            if gid not in self.tasks:
                self.tasks[gid] = asyncio.create_task(self.run_goal(gid), name=f'ancor:goal:{gid}')
                launched.append(gid)
        return launched

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
