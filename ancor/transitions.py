"""Goal-tree convergence before a request transition becomes durable."""
from __future__ import annotations

from .goals import (ACTIVE_GOAL_STATUSES, GoalStore, default_process_killer)
from .store import Store


def _result(store, request_id, activated=(), cascade=None):
    request = store.get_request(request_id)
    out = {
        'request_id': request_id,
        'outcome': 'completed',
        'status': request['status'],
        'target_status': request['target_status'],
        'activated_request_ids': [row['id'] for row in activated],
    }
    if cascade is not None:
        out['cascade'] = cascade
    return out


def _blocked(store, request_id, blockers):
    request = store.get_request(request_id)
    return {
        'request_id': request_id,
        'outcome': 'blocked',
        'status': request['status'],
        'target_status': request['target_status'],
        'blockers': blockers,
        'next_step': 'Resolve the reported blockers, then call advance again.',
    }


def advance_request(store, goal_store: GoalStore, request_id: str, *,
                    pause_mode: str = 'drain',
                    process_killer=default_process_killer,
                    job_canceller=None):
    """Cascade lifecycle down the request's L1 goal subtrees, then commit request status."""
    request = store.get_request(request_id)
    target = request.get('target_status')
    if target is None:
        return _result(store, request_id)

    l1_goals = goal_store.list_goals(
        request_id=request_id, parent_goal_id='root', include_terminal=False)

    if target == 'active':
        if request['status'] == 'pending':
            blockers = []
            for base_id in Store._replacement_ids(request['replacement_of']):
                base = store.get_request(base_id)
                if base['status'] != 'invalidated':
                    blockers.append({
                        'request_id': base_id,
                        'reason': 'replacement_base_not_invalidated',
                        'status': base['status'],
                        'target_status': base['target_status'],
                    })
            if blockers:
                return _blocked(store, request_id, blockers)
            finalized = store.finalize_transition(request_id)
            return _result(store, request_id, finalized['activated'])

        resumed = []
        for goal in l1_goals:
            if goal['status'] == 'paused' and goal['paused_by_ancestor'] is None:
                resumed.append(goal_store.resume_goal(goal['goal_id'], caller_goal_id='root'))
        finalized = store.finalize_transition(request_id)
        return _result(store, request_id, finalized['activated'], cascade=resumed)

    if target == 'paused':
        paused = []
        for goal in l1_goals:
            if goal['status'] in ACTIVE_GOAL_STATUSES and goal['status'] != 'paused':
                paused.append(goal_store.pause_goal(
                    goal['goal_id'], mode=pause_mode, caller_goal_id='root',
                    process_killer=process_killer, job_canceller=job_canceller))
        finalized = store.finalize_transition(request_id)
        return _result(store, request_id, finalized['activated'], cascade=paused)

    if target in {'cancelled', 'invalidated'}:
        reason = f'ANCOR request {request_id} transitioned to {target}'
        stopped = []
        for goal in l1_goals:
            stopped.append(goal_store.stop_goal(
                goal['goal_id'], reason=reason, caller_goal_id='root',
                process_killer=process_killer, job_canceller=job_canceller))
        finalized = store.finalize_transition(request_id)
        return _result(store, request_id, finalized['activated'], cascade=stopped)

    raise ValueError(f'unsupported request target {target!r}')
