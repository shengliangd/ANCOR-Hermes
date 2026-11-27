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
