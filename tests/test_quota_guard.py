"""Tests for generic QuotaGuard (arbitrary resource limits, allowlists, and unconstrained pass-through)."""
import os
import subprocess
import sys

import pytest

from ancor.goals import GoalStore, QuotaExceededError
from ancor.quota_guard import QuotaGuard
from ancor.store import Store
from ancor.watcher import FileClusterBackend


def _setup_tree(tmp_path):
    store = Store(tmp_path / 'requests.db')
    p = store.propose_change(
        None,
        [{'op': 'create', 'title': 'Generic Quota Test', 'fields': {'outcome': ['Run experiments']}}],
        actor='default',
        source={'kind': 'interactor_proposal', 'input': 'run'},
    )
    rid = store.commit_change(p['id'], 'confirm-1')['request_id']
    goals = GoalStore(store)
    goals.spawn_goal({
        'goal_id': 'l1',
        'request_ref': {'id': rid, 'generation': 1},
        'objective': 'L1', 'done_criteria': 'Done',
        'scope': {'readable_requests': [rid], 'can_ask_human': False},
        'budget': {
            'limits': {'gpu.alaya': 3, 'api_usd': 10, 'wall_hours': 8},
            'allowed': {'clusters': ['alaya'], 'writable_paths': [str(tmp_path / 'work')]},
            'constraints': ['Avoid saturating shared NAS'],
        },
    })
    goals.spawn_goal({
        'goal_id': 'l2_a',
        'request_ref': {'id': rid, 'generation': 1},
        'objective': 'L2A', 'done_criteria': 'Done',
        'scope': {'readable_requests': [rid], 'can_ask_human': False},
        'budget': {'limits': {'gpu.alaya': 2, 'api_usd': 6, 'wall_hours': 4}},
    }, caller_goal_id='l1')
    goals.spawn_goal({
        'goal_id': 'l2_b',
        'request_ref': {'id': rid, 'generation': 1},
        'objective': 'L2B', 'done_criteria': 'Done',
        'scope': {'readable_requests': [rid], 'can_ask_human': False},
        'budget': {'limits': {'gpu.alaya': 1, 'api_usd': 4, 'wall_hours': 4}},
    }, caller_goal_id='l1')
    backend = FileClusterBackend(tmp_path / 'cluster.json')
    return store, goals, backend


def test_generic_quota_guard_enforces_arbitrary_limits_and_allows_unconstrained(tmp_path, monkeypatch):
    store, goals, backend = _setup_tree(tmp_path)
    guard = QuotaGuard(goals, backend)

    monkeypatch.delenv('GOAL_ID', raising=False)
    with pytest.raises(PermissionError, match='GOAL_ID'):
        guard.acquire(usage={'gpu.alaya': 1})

    monkeypatch.setenv('GOAL_ID', 'l2_a')
    # Disallowed tag rejected
    with pytest.raises(QuotaExceededError, match='clusters'):
        guard.acquire(usage={'gpu.alaya': 1}, tags={'clusters': 'other-cluster'})

    # Acquire 2 gpu.alaya + 5 api_usd + unconstrained cpu_cores=16: succeeds!
    h1 = guard.acquire(
        usage={'gpu.alaya': 2, 'api_usd': 5, 'cpu_cores': 16},
        tags={'clusters': 'alaya', 'writable_paths': str(tmp_path / 'work' / 'run1')},
        handle_id='h1',
    )
    assert h1['handle_id'] == 'h1'
    assert backend.query_handle('h1')['status'] == 'running'

    # Extra 2 api_usd on l2_a exceeds l2_a api_usd=6 (5 + 2 > 6) -> rejected!
    with pytest.raises(QuotaExceededError, match="resource limit exceeded for 'api_usd'"):
        guard.acquire(usage={'api_usd': 2}, handle_id='h_over_usd')

    # Sibling l2_b acquires remaining 1 gpu.alaya + 4 api_usd -> succeeds (total 3 gpu, 9 usd)
    monkeypatch.setenv('GOAL_ID', 'l2_b')
    guard.acquire(usage={'gpu.alaya': 1, 'api_usd': 4}, tags={'clusters': 'alaya'}, handle_id='h2')
    assert goals.subtree_active_usage('l1')['gpu.alaya'] == 3
    assert goals.subtree_active_usage('l1')['api_usd'] == 9

    # Pause l1 -> any further acquisition in subtree rejected
    goals.pause_goal('l1', mode='drain', process_killer=lambda _: True)
    with pytest.raises(QuotaExceededError, match='paused'):
        guard.acquire(usage={'cpu_cores': 1}, handle_id='h_paused')


def test_generic_quota_guard_cli_wrapper(tmp_path):
    store, _goals, _backend = _setup_tree(tmp_path)
    env = {**os.environ, 'GOAL_ID': 'l2_b'}
    ok = subprocess.run(
        [sys.executable, '-m', 'ancor.quota_guard',
         '--store', str(store.path), '--use', 'gpu.alaya=1', '--use', 'api_usd=2.5',
         '--tag', 'clusters=alaya', '--handle-id', 'cli_h1'],
        env=env, capture_output=True, text=True,
    )
    assert ok.returncode == 0
    over = subprocess.run(
        [sys.executable, '-m', 'ancor.quota_guard',
         '--store', str(store.path), '--use', 'gpu.alaya=1', '--handle-id', 'cli_h2'],
        env=env, capture_output=True, text=True,
    )
    assert over.returncode == 2
    assert 'resource limit exceeded' in over.stderr
