"""Tests for sidecar active_goals store, generic 3-layer resource budgets, attenuation & cascade."""
import pytest

from ancor.goals import AttenuationError, GoalStore
from ancor.store import Store


def _make_request(store: Store, title: str = 'Dexterous Hand Recipe') -> dict:
    proposal = store.propose_change(
        None,
        [{'op': 'create', 'title': title, 'fields': {
            'outcome': ['Improve closed-loop success rate by >= 5%'],
            'constraint': ['Stay within allocated budget'],
            'context': ['/tmp/hand-exp'],
        }}],
        actor='default',
        source={'kind': 'interactor_proposal', 'input': title},
    )
    res = store.commit_change(proposal['id'], 'confirm-ref-1')
    return store.get_request(res['request_id'])


def test_generic_three_layer_budget_and_monotonic_attenuation(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    req2 = _make_request(store, 'Second Request')
    now = [1000.0]
    goals = GoalStore(store, clock=lambda: now[0], global_limits={'gpu.alaya': 4, 'api_usd': 50})

    l1_yaml = f"""
goal_id: g_hand_01
parent_goal_id: root
request_ref:
  id: "{req['id']}"
  generation: {req['generation']}
objective: "Validate new dexterous hand data recipe"
done_criteria: "Provide matched eval comparison or infeasibility evidence"
scope:
  readable_requests: ["{req['id']}"]
  can_ask_human: false
budget:
  limits:
    gpu.alaya: 3
    api_usd: 20
    disk_gb: 200
    wall_hours: 12
  allowed:
    clusters: ["alaya", "dlc"]
    writable_paths: ["{tmp_path / 'hand'}"]
    accounts: ["dev-ro"]
  constraints:
    - "Prefer off-peak hours for large eval sweeps"
"""
    l1 = goals.spawn_goal(l1_yaml, caller_goal_id='root')
    assert l1['goal_id'] == 'g_hand_01'
    assert l1['budget']['limits']['gpu.alaya'] == 3
    assert l1['budget']['limits']['api_usd'] == 20
    assert l1['budget']['constraints'] == ['Prefer off-peak hours for large eval sweeps']

    # Child 1 takes 2 gpu.alaya + 12 api_usd + subpath + appends its own soft constraint
    c1 = goals.spawn_goal({
        'goal_id': 'g_hand_01_a',
        'request_ref': {'id': req['id'], 'generation': req['generation']},
        'objective': 'Clean & balance contact-force dataset',
        'done_criteria': 'Balanced dataset saved',
        'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
        'budget': {
            'limits': {'gpu.alaya': 2, 'api_usd': 12, 'wall_hours': 6},
            'allowed': {'clusters': ['alaya'], 'writable_paths': [str(tmp_path / 'hand' / 'data')]},
            'constraints': ['Check local cache before downloading weights'],
        },
    }, caller_goal_id='g_hand_01')
    # Natural-language constraints auto-inherited + appended!
    assert c1['budget']['constraints'] == [
        'Prefer off-peak hours for large eval sweeps',
        'Check local cache before downloading weights',
    ]
    # Omitted parent allowlist ('accounts') is inherited unchanged!
    assert c1['budget']['allowed']['accounts'] == ['dev-ro']
    # Omitted additive parent limit ('disk_gb') defaults to 0 so child cannot bypass parent!
    assert c1['budget']['limits']['disk_gb'] == 0

    # Child 2 requesting 2 gpu.alaya exceeds parent limit 3 (2 + 2 > 3): REJECTED!
    with pytest.raises(AttenuationError, match="limit 'gpu.alaya'"):
        goals.spawn_goal({
            'goal_id': 'g_hand_01_b',
            'request_ref': {'id': req['id'], 'generation': req['generation']},
            'objective': 'Run baseline eval',
            'done_criteria': 'Baseline JSON ready',
            'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
            'budget': {'limits': {'gpu.alaya': 2, 'api_usd': 5, 'wall_hours': 4}},
        }, caller_goal_id='g_hand_01')

    # Child 2 requesting 10 api_usd exceeds parent api_usd=20 (12 + 10 > 20): REJECTED!
    with pytest.raises(AttenuationError, match="limit 'api_usd'"):
        goals.spawn_goal({
            'goal_id': 'g_hand_01_usd',
            'request_ref': {'id': req['id'], 'generation': req['generation']},
            'objective': 'Expensive API calls',
            'done_criteria': 'Never',
            'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
            'budget': {'limits': {'gpu.alaya': 1, 'api_usd': 10, 'wall_hours': 4}},
        }, caller_goal_id='g_hand_01')

    # Path escape outside parent writable_paths: REJECTED!
    with pytest.raises(AttenuationError, match='outside parent'):
        goals.spawn_goal({
            'goal_id': 'g_hand_escape',
            'request_ref': {'id': req['id'], 'generation': req['generation']},
            'objective': 'Escape path',
            'done_criteria': 'Never',
            'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
            'budget': {'allowed': {'writable_paths': [str(tmp_path / 'other')]}},
        }, caller_goal_id='g_hand_01')

    # Foreign readable_requests: REJECTED!
    with pytest.raises(AttenuationError, match='readable_requests'):
        goals.spawn_goal({
            'goal_id': 'g_hand_foreign',
            'request_ref': {'id': req['id'], 'generation': req['generation']},
            'objective': 'Read foreign',
            'done_criteria': 'Never',
            'scope': {'readable_requests': [req['id'], req2['id']], 'can_ask_human': False},
            'budget': {},
        }, caller_goal_id='g_hand_01')

    # Child 2 within remaining limits (1 gpu.alaya + 8 api_usd): OK!
    c2 = goals.spawn_goal({
        'goal_id': 'g_hand_01_b',
        'request_ref': {'id': req['id'], 'generation': req['generation']},
        'objective': 'Run baseline eval',
        'done_criteria': 'Baseline JSON ready',
        'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
        'budget': {'limits': {'gpu.alaya': 1, 'api_usd': 8, 'wall_hours': 4}},
    }, caller_goal_id='g_hand_01')
    assert c2['budget']['limits']['api_usd'] == 8


def test_direct_authoritative_db_read_and_generation_propagation(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req1 = _make_request(store, 'Req 1')
    req2 = _make_request(store, 'Req 2')
    goals = GoalStore(store)

    l1 = goals.spawn_goal({
        'goal_id': 'g_l1',
        'request_ref': {'id': req1['id'], 'generation': 1},
        'objective': 'Work on Req 1',
        'done_criteria': 'Done',
        'scope': {'readable_requests': [req1['id']], 'can_ask_human': False},
        'budget': {'limits': {'gpu.alaya': 2, 'wall_hours': 4}},
    })

    read = goals.read_request_for_goal(l1['goal_id'], req1['id'])
    assert read['generation'] == 1
    with pytest.raises(PermissionError):
        goals.read_request_for_goal(l1['goal_id'], req2['id'])

    req1_cur = store.get_request(req1['id'])
    prop = store.propose_change(
        req1['id'],
        [{'op': 'append', 'field': 'constraint', 'body': 'Must also evaluate on unseen gripper'}],
        actor='default',
        source={'kind': 'interactor_proposal', 'input': 'add gripper'},
        base={'generation': req1_cur['generation'], 'control_version': req1_cur['control_version']},
    )
    store.commit_change(prop['id'], 'confirm-ref-2')
    assert store.get_request(req1['id'])['generation'] == 2

    updated = goals.signal_goal(l1['goal_id'], 'update_generation', {'generation': 2, 'message': 'New constraint added'})
    assert updated['generation'] == 2
    assert updated['needs_wake'] is True
    read_v2 = goals.read_request_for_goal(l1['goal_id'], req1['id'])
    assert read_v2['generation'] == 2
    assert len(read_v2['fields']['constraint']) == 2


def test_deterministic_tree_cascade_pause_resume_stop(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store)

    def mk(gid, parent, gpus=1):
        return goals.spawn_goal({
            'goal_id': gid,
            'request_ref': {'id': req['id'], 'generation': 1},
            'objective': f'Obj {gid}',
            'done_criteria': 'Done',
            'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
            'budget': {'limits': {'gpu.alaya': gpus, 'wall_hours': 8}, 'allowed': {'clusters': ['alaya']}},
        }, caller_goal_id=parent)

    mk('l1', 'root', gpus=4)
    mk('l2_active', 'l1', gpus=2)
    mk('l2_self_paused', 'l1', gpus=1)
    mk('l2_done', 'l1', gpus=1)
    mk('l3_under_active', 'l2_active', gpus=1)

    goals.set_process('l1', 1001)
    goals.set_process('l2_active', 1002)
    goals.set_process('l3_under_active', 1003)
    goals.register_resource_usage('l3_under_active', handle_id='job_a',
                                  usage={'gpu.alaya': 1}, tags={'clusters': 'alaya'})

    goals.report_result('l2_done', {
        'request_generation': 1, 'status': 'completed',
        'summary': 'finished early', 'artifacts': [], 'metrics': {}, 'open_issues': [],
    })
    killed = []
    cancelled_jobs = []
    goals.pause_goal('l2_self_paused', mode='drain', caller_goal_id='l1',
                     process_killer=lambda p: killed.append(p) or True)

    res_drain = goals.pause_goal(
        'l1', mode='drain', caller_goal_id='root',
        process_killer=lambda p: killed.append(p) or True,
        job_canceller=lambda j: cancelled_jobs.append(j['handle_id']) or True,
    )
    assert set(res_drain['paused_goals']) == {'l1', 'l2_active', 'l3_under_active'}
    assert set(res_drain['killed_pids']) == {1001, 1002, 1003}
    assert res_drain['cancelled_jobs'] == []
    assert goals.get_goal('l2_active')['paused_by_ancestor'] == 'l1'
    assert goals.get_goal('l2_self_paused')['paused_by_ancestor'] is None
    assert goals.get_goal('l2_done')['status'] == 'completed'

    with pytest.raises(RuntimeError, match='ancestor l1 is still paused'):
        goals.resume_goal('l2_active', caller_goal_id='l1')

    res_resume = goals.resume_goal('l1', caller_goal_id='root')
    assert set(res_resume['resumed_goals']) == {'l1', 'l2_active', 'l3_under_active'}
    assert goals.get_goal('l2_self_paused')['status'] == 'paused'
    assert goals.get_goal('l3_under_active')['status'] == 'waiting_external'

    res_preempt = goals.pause_goal(
        'l1', mode='preempt', caller_goal_id='root',
        process_killer=lambda p: True,
        job_canceller=lambda j: cancelled_jobs.append(j['handle_id']) or True,
    )
    assert res_preempt['cancelled_jobs'] == ['job_a']

    res_stop = goals.stop_goal(
        'l1', reason='cancelled by user', caller_goal_id='root',
        process_killer=lambda p: True,
        job_canceller=lambda j: cancelled_jobs.append(j['handle_id']) or True,
    )
    assert set(res_stop['cancelled_goals']) == {'l1', 'l2_active', 'l2_self_paused', 'l3_under_active'}
    assert goals.get_goal('l2_done')['status'] == 'completed'


def _repair_spec(req, goal_id, limits=None):
    return {
        'goal_id': goal_id,
        'request_ref': {'id': req['id'], 'generation': req['generation']},
        'objective': 'Execute work', 'done_criteria': 'Evidence supplied',
        'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
        'budget': {'limits': limits or {}},
    }


def test_global_limits_survive_unconfigured_worker_and_omitted_allocation(tmp_path):
    from ancor.goals import QuotaExceededError
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    host = GoalStore(store, global_limits={'gpu': 1})
    goal = host.spawn_goal(_repair_spec(req, 'no_allocation'))
    assert goal['budget']['limits']['gpu'] == 0
    worker = GoalStore(Store(store.path))
    with pytest.raises(QuotaExceededError):
        worker.register_resource_usage('no_allocation', handle_id='h', usage={'gpu': 1})
    with pytest.raises(AttenuationError):
        worker.spawn_goal(_repair_spec(req, 'too_large', {'gpu': 2}))


def test_global_usage_counts_live_handles_on_completed_goals(tmp_path):
    from ancor.goals import QuotaExceededError
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store, global_limits={'gpu': 1})
    goals.spawn_goal(_repair_spec(req, 'old', {'gpu': 1}))
    goals.register_resource_usage('old', handle_id='still_running', usage={'gpu': 1})
    goals.report_result('old', {'request_generation': 1, 'status': 'completed', 'summary': 'done'})
    goals.spawn_goal(_repair_spec(req, 'new', {'gpu': 1}))
    with pytest.raises(QuotaExceededError, match='global'):
        GoalStore(store).register_resource_usage('new', handle_id='new_job', usage={'gpu': 1})


def test_stale_receipt_preserves_updated_goal_and_wakeup(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store)
    goals.spawn_goal(_repair_spec(req, 'updated'))
    goals.signal_goal('updated', 'update_generation', {'generation': 2})
    before = goals.get_goal('updated')
    with pytest.raises(RuntimeError, match='generation'):
        goals.report_result('updated', {'request_generation': 1, 'status': 'completed', 'summary': 'old work'})
    after = goals.get_goal('updated')
    assert after == before
    assert after['status'] == 'running' and after['needs_wake']
    assert after['result'] is None


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_global_limits_and_usage_rejected(tmp_path, value):
    store = Store(tmp_path / 'requests.db')
    with pytest.raises(ValueError):
        GoalStore(store, global_limits={'gpu': value})
    req = _make_request(store)
    goals = GoalStore(store)
    goals.spawn_goal(_repair_spec(req, 'finite'))
    with pytest.raises(ValueError):
        goals.register_resource_usage('finite', handle_id='invalid', usage={'gpu': value})


def test_pause_confirms_sigterm_ignoring_process_exit(tmp_path):
    import subprocess
    import sys
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store)
    goals.spawn_goal(_repair_spec(req, 'stubborn'))
    proc = subprocess.Popen([sys.executable, '-u', '-c',
        'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready"); time.sleep(60)'],
        start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == 'ready'
        goals.set_process('stubborn', proc.pid)
        result = goals.pause_goal('stubborn')
        assert result['stop_confirmed']
        assert result['killed_pids'] == [proc.pid]
        assert proc.wait(timeout=2) != 0
        assert goals.get_goal('stubborn')['pid'] is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


def test_failed_stop_retains_handles_and_retries_after_restart(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store)
    goals.spawn_goal(_repair_spec(req, 'pending_stop'))
    goals.set_process('pending_stop', 12345)
    goals.register_resource_usage('pending_stop', handle_id='external', usage={'gpu': 1})
    result = goals.pause_goal('pending_stop', mode='preempt', process_killer=lambda _: False,
                              job_canceller=lambda _: False)
    assert not result['stop_confirmed']
    current = goals.get_goal('pending_stop')
    assert current['status'] == 'paused' and current['pid'] == 12345
    assert current['external_job_ids'][0]['status'] == 'running'
    with pytest.raises(RuntimeError, match='unconfirmed'):
        goals.resume_goal('pending_stop')
    reopened = GoalStore(Store(store.path))
    result = reopened.settle_stops(process_killer=lambda _: True, job_canceller=lambda _: True)
    assert result['stop_confirmed'] and result['cancelled_jobs'] == ['external']
    assert reopened.get_goal('pending_stop')['pid'] is None
    reopened.resume_goal('pending_stop')
    assert reopened.get_goal('pending_stop')['needs_wake']


def test_native_stop_failure_remains_pending_after_cli_exit(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store)
    goals.spawn_goal(_repair_spec(req, 'native'))
    goals.bind_session('native', 'actual-native-session')
    result = goals.pause_goal('native', session_stopper=lambda _: False)
    assert not result['stop_confirmed']
    assert goals.get_goal('native')['session_id'] == 'actual-native-session'
    with pytest.raises(RuntimeError, match='unconfirmed'):
        goals.resume_goal('native')
    reopened = GoalStore(Store(store.path))
    assert reopened.settle_stops(session_stopper=lambda _: True)['stop_confirmed']
    reopened.resume_goal('native')


def test_concurrent_resource_reservations_are_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from ancor.goals import QuotaExceededError
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store, global_limits={'gpu': 1})
    goals.spawn_goal(_repair_spec(req, 'limited', {'gpu': 1}))
    def acquire(index):
        try:
            GoalStore(Store(store.path)).register_resource_usage('limited', handle_id=str(index), usage={'gpu': 1})
            return True
        except QuotaExceededError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(acquire, (1, 2))) == [False, True]
    assert goals.subtree_active_usage('limited') == {'gpu': 1}
