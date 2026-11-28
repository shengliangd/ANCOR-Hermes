"""Tests for zero-LLM GoalWatcher, Crash-Safe GoalReconciler, and GoalRunner (2-profile Plan A)."""
import json
import sys

from ancor.goals import GoalStore
from ancor.quota_guard import QuotaGuard
from ancor.store import Store
from ancor.watcher import FileClusterBackend, GoalReconciler, GoalRunner, GoalWatcher


def _create_goal(tmp_path, now_box):
    store = Store(tmp_path / 'requests.db')
    p = store.propose_change(
        None,
        [{'op': 'create', 'title': 'Watcher Test', 'fields': {'outcome': ['Run cluster job']}}],
        actor='default',
        source={'kind': 'interactor_proposal', 'input': 'run job'},
    )
    rid = store.commit_change(p['id'], 'confirm-1')['request_id']
    goals = GoalStore(store, clock=lambda: now_box[0])
    backend = FileClusterBackend(tmp_path / 'cluster.json', clock=lambda: now_box[0])
    g = goals.spawn_goal({
        'goal_id': 'g_watch',
        'request_ref': {'id': rid, 'generation': 1},
        'objective': 'Train & eval',
        'done_criteria': 'Eval done',
        'scope': {'readable_requests': [rid], 'can_ask_human': False},
        'budget': {'limits': {'gpu.alaya': 2, 'wall_hours': 2}, 'allowed': {'clusters': ['alaya']}},
    })
    return store, goals, backend, g


def test_watcher_zero_llm_poll_and_drain_pause_semantics(tmp_path):
    now = [1000.0]
    _store, goals, backend, _g = _create_goal(tmp_path, now)
    guard = QuotaGuard(goals, backend)
    guard.acquire(usage={'gpu.alaya': 2}, tags={'clusters': 'alaya'}, handle_id='dlc_1', goal_id='g_watch')
    goals.mark_waiting_external('g_watch')

    watcher = GoalWatcher(goals, backend, clock=lambda: now[0])
    assert watcher.poll() == {'transitions': [], 'timeouts': [], 'woken_goals': []}

    goals.pause_goal('g_watch', mode='drain', process_killer=lambda _: True)
    backend.set_status('dlc_1', 'succeeded', detail='val_sr=0.74')
    poll2 = watcher.poll()
    assert len(poll2['transitions']) == 1
    assert poll2['woken_goals'] == []
    paused_goal = goals.get_goal('g_watch')
    assert paused_goal['status'] == 'paused'
    assert paused_goal['needs_wake'] is False
    assert paused_goal['pending_signals'][-1]['payload']['handle_id'] == 'dlc_1'

    goals.resume_goal('g_watch')
    resumed = goals.get_goal('g_watch')
    assert resumed['status'] == 'running'
    assert resumed['needs_wake'] is True


def test_watcher_wakes_on_handle_transition_and_wall_clock_timeout(tmp_path):
    now = [1000.0]
    _store, goals, backend, _g = _create_goal(tmp_path, now)
    guard = QuotaGuard(goals, backend)
    guard.acquire(usage={'gpu.alaya': 1}, tags={'clusters': 'alaya'}, handle_id='dlc_2', goal_id='g_watch')
    goals.mark_waiting_external('g_watch')

    watcher = GoalWatcher(goals, backend, clock=lambda: now[0])
    backend.set_status('dlc_2', 'failed', detail='OOM')
    poll = watcher.poll()
    assert poll['woken_goals'] == ['g_watch']
    g = goals.get_goal('g_watch')
    assert g['status'] == 'running'
    assert g['pending_signals'][-1]['payload']['status'] == 'failed'

    goals.consume_signals('g_watch')
    now[0] += 3 * 3600
    assert len(watcher.poll()['timeouts']) == 1
    assert watcher.poll()['timeouts'] == []


def test_crash_safe_reconciler(tmp_path):
    now = [2000.0]
    _store, goals, backend, _g = _create_goal(tmp_path, now)
    rid = goals.get_goal('g_watch')['request_id']
    guard = QuotaGuard(goals, backend)

    guard.acquire(usage={'gpu.alaya': 1}, tags={'clusters': 'alaya'}, handle_id='cloud_j1', goal_id='g_watch')
    goals.mark_waiting_external('g_watch')

    goals.spawn_goal({
        'goal_id': 'g_crashed',
        'request_ref': {'id': rid, 'generation': 1},
        'objective': 'Mid-turn script',
        'done_criteria': 'Done',
        'scope': {'readable_requests': [rid], 'can_ask_human': False},
        'budget': {'limits': {'gpu.alaya': 1, 'wall_hours': 4}, 'allowed': {'clusters': ['alaya']}},
    })
    guard.acquire(usage={'gpu.alaya': 1}, tags={'clusters': 'alaya'}, handle_id='cloud_j2', goal_id='g_crashed')
    goals.set_process('g_crashed', 999999)

    reconciler = GoalReconciler(goals, backend, clock=lambda: now[0], pid_alive=lambda pid: False)
    report = reconciler.reconcile()
    assert report['waiting_external'] == ['g_watch']
    assert report['recovered_running'] == ['g_crashed']
    g_rec = goals.get_goal('g_crashed')
    assert g_rec['pid'] is None and g_rec['needs_wake'] is True
    assert g_rec['pending_signals'][-1]['payload']['external_handles'][0]['handle_id'] == 'cloud_j2'


def test_goal_runner_launches_under_ancor_exec_with_role_and_goal_id(tmp_path):
    now = [3000.0]
    _store, goals, _backend, _g = _create_goal(tmp_path, now)
    out_file = tmp_path / 'child_env.json'

    def toy_cmd(profile, session_id, prompt, *, resume, model_args=()):
        code = (
            'import json, os, pathlib; '
            f'pathlib.Path({str(out_file)!r}).write_text(json.dumps({{'
            '"goal_id": os.environ.get("GOAL_ID"), '
            '"ancor_role": os.environ.get("ANCOR_ROLE"), '
            f'"profile": {profile!r}, "session_id": {session_id!r}'
            '}))'
        )
        return [sys.executable, '-c', code]

    runner = GoalRunner(goals, command_builder=toy_cmd)
    assert runner.run_goal_sync('g_watch') == 0
    recorded = json.loads(out_file.read_text())
    assert recorded == {
        'goal_id': 'g_watch',
        'ancor_role': 'goal',
        'profile': 'ancor-exec',
        'session_id': '',
    }


def test_dispatch_returns_with_live_process_and_can_launch_more_work(tmp_path):
    import asyncio
    import os
    import pytest

    _, goals, _, goal = _create_goal(tmp_path, [3000.0])

    async def scenario():
        runner = GoalRunner(goals, command_builder=lambda *a, **kw: [
            sys.executable, '-c', 'import time; time.sleep(60)'])
        pids = []
        try:
            assert await asyncio.wait_for(runner.dispatch_ready(), 1) == ['g_watch']
            # Prevent duplicate launch even before the first task stores its PID.
            assert await runner.dispatch_ready() == []
            for _ in range(100):
                pid = goals.get_goal('g_watch')['pid']
                if pid:
                    break
                await asyncio.sleep(.01)
            assert pid
            pids.append(pid)
            os.kill(pid, 0)
            goals.spawn_goal({
                'goal_id': 'second', 'request_ref': goal['request_ref'],
                'objective': 'Concurrent work', 'done_criteria': 'Done',
                'scope': goal['scope'], 'budget': {},
            })
            assert await asyncio.wait_for(runner.dispatch_ready(), 1) == ['second']
            for _ in range(100):
                pid = goals.get_goal('second')['pid']
                if pid:
                    break
                await asyncio.sleep(.01)
            assert pid
            pids.append(pid)
            os.kill(pids[0], 0)
        finally:
            await asyncio.wait_for(runner.close(), 7)
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert goals.get_goal('g_watch')['pid'] is None
        assert goals.get_goal('second')['pid'] is None

    asyncio.run(scenario())


def test_process_failure_retries_preserve_signals_and_eventually_notify_root(tmp_path):
    now = [4000.0]
    store, goals, _, _ = _create_goal(tmp_path, now)
    goals.signal_goal('g_watch', 'steer', {'message': 'do not lose this'})
    runner = GoalRunner(goals, command_builder=lambda *a, **kw: [sys.executable, '-c', 'raise SystemExit(2)'])
    for attempt in range(3):
        assert runner.run_goal_sync('g_watch') == 2
        current = goals.get_goal('g_watch')
        assert current['pid'] is None
        assert any(s['payload'].get('message') == 'do not lose this' for s in current['pending_signals'])
        if attempt < 2:
            assert current['status'] == 'running' and current['needs_wake']
            assert goals.goals_needing_wake() == []
            now[0] += 60
            # Retry survives reopening the store / restarting the supervisor.
            assert GoalStore(Store(store.path), clock=lambda: now[0]).goals_needing_wake()[0]['goal_id'] == 'g_watch'
        else:
            assert current['status'] == 'blocked'
            assert 'code 2' in current['result']['open_issues'][0]
    assert len([e for e in store.pending_events() if e['kind'] == 'goal.blocked']) == 1


def test_launch_failure_and_zero_exit_without_report_are_recoverable(tmp_path):
    import pytest
    _, goals, _, _ = _create_goal(tmp_path, [4000.0])
    goals.signal_goal('g_watch', 'steer', {'message': 'retain work'})
    runner = GoalRunner(goals, command_builder=lambda *a, **kw: ['/no/such/executable'])
    with pytest.raises(FileNotFoundError):
        runner.run_goal_sync('g_watch')
    current = goals.get_goal('g_watch')
    assert current['needs_wake'] and current['status'] == 'running'
    assert current['pending_signals'][0]['payload']['message'] == 'retain work'
    runner = GoalRunner(goals, command_builder=lambda *a, **kw: [sys.executable, '-c', 'pass'])
    assert runner.run_goal_sync('g_watch') == 0
    assert goals.get_goal('g_watch')['needs_wake']


def test_watcher_preserves_concurrent_new_signals_and_handles(tmp_path):
    _, goals, backend, _ = _create_goal(tmp_path, [1000.0])
    goals.register_resource_usage('g_watch', handle_id='first')
    backend.submit_handle(handle_id='first', goal_id='g_watch', usage={}, tags={})
    backend.set_status('first', 'succeeded')
    original = backend.query_handle
    def query(handle_id, tags=None):
        goals.signal_goal('g_watch', 'steer', {'message': 'arrived during poll'})
        goals.register_resource_usage('g_watch', handle_id='second')
        return original(handle_id, tags)
    backend.query_handle = query
    GoalWatcher(goals, backend).poll()
    current = goals.get_goal('g_watch')
    assert {h['handle_id'] for h in current['external_job_ids']} == {'first', 'second'}
    assert any(s['payload'].get('message') == 'arrived during poll' for s in current['pending_signals'])


def test_reconciler_discovers_later_exit_of_orphan_without_repeating_wakeup(tmp_path):
    _, goals, backend, _ = _create_goal(tmp_path, [1000.0])
    goals.set_process('g_watch', 99999)
    alive = [True]
    reconciler = GoalReconciler(goals, backend, pid_alive=lambda _: alive[0])
    assert reconciler.reconcile()['still_alive'] == ['g_watch']
    alive[0] = False
    assert reconciler.reconcile()['recovered_running'] == ['g_watch']
    assert reconciler.reconcile()['recovered_running'] == []
    assert len(goals.get_goal('g_watch')['pending_signals']) == 1


def test_wait_goal_wakes_on_timer_and_watch_path(tmp_path):
    now = [1000.0]
    _, goals, backend, _ = _create_goal(tmp_path, now)
    marker = tmp_path / 'done.flag'
    res = goals.wait_goal('g_watch', wake_after_seconds=600, watch_path=str(marker), reason='wait for eval')
    assert res['status'] == 'waiting_external'
    assert goals.get_goal('g_watch')['status'] == 'waiting_external'
    watcher = GoalWatcher(goals, backend, clock=lambda: now[0])
    assert watcher.poll()['woken_goals'] == []
    marker.write_text('ok')
    polled = watcher.poll()
    assert polled['woken_goals'] == ['g_watch']
    assert any(t['event'] == 'watch_path_ready' for t in polled['transitions'])
    assert goals.get_goal('g_watch')['status'] == 'running'
    assert goals.get_goal('g_watch')['needs_wake'] is True

    # Timer wake
    goals.consume_signals('g_watch')
    goals.wait_goal('g_watch', wake_after_seconds=300, reason='check again')
    now[0] = 1301.0
    polled2 = watcher.poll()
    assert polled2['woken_goals'] == ['g_watch']
    assert any(t['event'] == 'scheduled_wake' for t in polled2['transitions'])


def test_resource_release_and_terminal_goal_auto_release(tmp_path):
    now = [1000.0]
    _, goals, _, _ = _create_goal(tmp_path, now)
    goals.register_resource_usage('g_watch', handle_id='h1', usage={'gpu': 1})
    goals.register_resource_usage('g_watch', handle_id='h2', usage={'gpu': 1})
    assert goals.total_active_usage()['active_usage']['gpu'] == 2.0

    rel = goals.release_resource_usage('g_watch', handle_id='h1', status='succeeded')
    assert rel['released'] == ['h1']
    assert goals.total_active_usage()['active_usage']['gpu'] == 1.0

    goals.report_result('g_watch', {
        'goal_id': 'g_watch', 'request_generation': 1,
        'status': 'completed', 'summary': 'done',
    })
    # Unreleased live handle on completed goal stays accounted until explicitly released
    assert goals.total_active_usage()['active_usage']['gpu'] == 1.0
    rel2 = goals.release_resource_usage('g_watch', status='released', detail='confirmed stopped')
    assert rel2['released'] == ['h2']
    assert goals.total_active_usage()['active_usage'] == {}
    assert goals.subtree_active_usage('g_watch') == {}


def test_update_goal_budget_dynamically(tmp_path):
    now = [1000.0]
    _, goals, _, _ = _create_goal(tmp_path, now)
    updated = goals.update_goal_budget(
        'g_watch', {'limits': {'gpu': 1}, 'constraints': ['yield 1 GPU']}, reason='yield to high priority')
    assert updated['budget']['limits']['gpu'] == 1
    assert updated['needs_wake'] is True
    assert any(s['kind'] == 'steer' for s in updated['pending_signals'])


def test_blocked_goal_can_be_unblocked_in_place_by_parent(tmp_path):
    import pytest
    now = [1000.0]
    _, goals, _, _ = _create_goal(tmp_path, now)
    goals.bind_session('g_watch', 'sess-keep-context')
    goals.report_result('g_watch', {
        'goal_id': 'g_watch', 'request_generation': 1,
        'status': 'blocked', 'summary': 'need new path', 'open_issues': ['missing path'],
    })
    assert goals.get_goal('g_watch')['status'] == 'blocked'

    # 1. Unblock via signal(steer) — keeps session_id and resumes
    revived = goals.signal_goal('g_watch', 'steer', {'message': 'use /data/v2'})
    assert revived['status'] == 'running'
    assert revived['needs_wake'] is True
    assert revived['session_id'] == 'sess-keep-context'
    assert revived['result'] is None

    # 2. Block again and unblock via resume_goal (verify second goal.blocked event is emitted)
    now[0] += 10.0
    goals.report_result('g_watch', {
        'goal_id': 'g_watch', 'request_generation': 1,
        'status': 'blocked', 'summary': 'blocked again', 'open_issues': ['still blocked'],
    })
    res = goals.resume_goal('g_watch')
    assert res['resumed_goals'] == ['g_watch']
    assert goals.get_goal('g_watch')['status'] == 'running'
    assert goals.get_goal('g_watch')['session_id'] == 'sess-keep-context'

    # 3. Block again and abandon via stop_goal -> cancelled (truly terminal)
    now[0] += 10.0
    goals.report_result('g_watch', {
        'goal_id': 'g_watch', 'request_generation': 1,
        'status': 'blocked', 'summary': 'abandon me', 'open_issues': ['abort'],
    })
    stopped = goals.stop_goal('g_watch', reason='no longer needed')
    assert 'g_watch' in stopped['cancelled_goals']
    assert goals.get_goal('g_watch')['status'] == 'cancelled'
    with pytest.raises(RuntimeError):
        goals.signal_goal('g_watch', 'steer', {'message': 'too late'})
