"""Native-card convergence before a requirement transition becomes durable."""
import os

from .store import STABLE_STATUSES, Store


SAFE_PAUSED_TASK_STATUSES = frozenset({
    'todo', 'triage', 'review', 'blocked', 'done', 'archived', 'cancelled',
})


def require_quiescence(detail):
    """Require every retained run to have ended without a live worker process."""
    for run in detail['runs']:
        pid = run.get('worker_pid')
        if pid:
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                continue
            except (PermissionError, ValueError, OSError):
                pass
            raise RuntimeError('worker quiescence not established')
        if not run.get('ended_at'):
            raise RuntimeError('worker quiescence not established')


def _result(store, requirement_id, activated=()):
    requirement = store.get_requirement(requirement_id)
    return {
        'requirement_id': requirement_id,
        'outcome': 'completed',
        'status': requirement['status'],
        'target_status': requirement['target_status'],
        'activated_requirement_ids': [row['id'] for row in activated],
    }


def _blocked(store, requirement_id, blockers):
    requirement = store.get_requirement(requirement_id)
    return {
        'requirement_id': requirement_id,
        'outcome': 'blocked',
        'status': requirement['status'],
        'target_status': requirement['target_status'],
        'blockers': blockers,
        'next_step': 'Resolve the reported card or replacement blockers, then call advance again.',
    }


def _shared_stable_requirements(snapshot, requirement_id, task_id):
    requirements = {row['id']: row for row in snapshot['requirements']}
    shared = []
    for link in snapshot['task_links']:
        if link['task_id'] != task_id or link['requirement_id'] == requirement_id:
            continue
        other = requirements.get(link['requirement_id'])
        if (other is not None and other['status'] in STABLE_STATUSES
                and other.get('target_status') is None):
            shared.append(other['id'])
    return shared


def _archive_linked(store, board, requirement_id, target):
    snapshot = store.snapshot()
    requirement = store.get_requirement(requirement_id)
    tasks = {task['id']: task for task in board.list()}
    linked = [link['task_id'] for link in snapshot['task_links']
              if link['requirement_id'] == requirement_id]
    replacement_id = None
    if target == 'invalidated':
        for candidate in snapshot['requirements']:
            if (candidate['status'] == 'pending'
                    and candidate.get('target_status') == 'active'
                    and requirement_id in Store._replacement_ids(candidate['replacement_of'])):
                replacement_id = candidate['id']
                break
    blockers = []
    for task_id in linked:
        task = tasks.get(task_id)
        if task is None:
            blockers.append({'task_id': task_id, 'reason': 'task_missing'})
            continue
        if task.get('status') == 'archived':
            continue
        shared = _shared_stable_requirements(snapshot, requirement_id, task_id)
        if shared:
            blockers.append({'task_id': task_id,
                             'reason': 'task_shared_with_stable_requirement',
                             'requirement_ids': shared})
            continue
        if target == 'invalidated' and replacement_id is not None:
            reason = (f'ANCOR requirement {requirement_id} is invalidated by '
                      f'{replacement_id}; archive this superseded card.')
        else:
            reason = (f'ANCOR requirement {requirement_id} is cancelled; '
                      'archive this card.')
        try:
            actual = board.archive(task_id, reason)
        except Exception as exc:
            blockers.append({'task_id': task_id, 'reason': 'archive_failed',
                             'detail': str(exc)[:240]})
            continue
        if actual.get('status') != 'archived':
            blockers.append({'task_id': task_id, 'reason': 'archive_not_verified'})
    if blockers:
        return blockers
    return store.finalize_transition(requirement_id)


def _pause_linked(store, board, requirement_id):
    tasks = {task['id']: task for task in board.list()}
    linked = [link['task_id'] for link in store.snapshot()['task_links']
              if link['requirement_id'] == requirement_id]
    blockers = []
    for task_id in linked:
        task = tasks.get(task_id)
        if task is None:
            blockers.append({'task_id': task_id, 'reason': 'task_missing'})
            continue
        status = task.get('status')
        if status in {'running', 'ready'}:
            try:
                board.block(task_id, f'ANCOR requirement {requirement_id} is paused; '
                                     'stop this card.')
            except Exception as exc:
                blockers.append({'task_id': task_id, 'reason': 'block_failed',
                                 'detail': str(exc)[:240]})
                continue
        try:
            detail = board._call('show', task_id)
            require_quiescence(detail)
            status = detail['task'].get('status')
        except Exception as exc:
            blockers.append({'task_id': task_id,
                             'reason': 'worker_quiescence_not_established',
                             'detail': str(exc)[:240]})
            continue
        if status not in SAFE_PAUSED_TASK_STATUSES:
            blockers.append({'task_id': task_id, 'reason': 'task_not_quiescent',
                             'task_status': status})
    if blockers:
        return blockers
    return store.finalize_transition(requirement_id)


def advance_requirement(store, board, requirement_id):
    """Converge linked native cards, then commit the requested requirement status."""
    requirement = store.get_requirement(requirement_id)
    target = requirement.get('target_status')
    if target is None:
        return _result(store, requirement_id)
    if target == 'active':
        if requirement['status'] == 'pending':
            blockers = []
            for base_id in Store._replacement_ids(requirement['replacement_of']):
                base = store.get_requirement(base_id)
                if base['status'] != 'invalidated':
                    blockers.append({
                        'requirement_id': base_id,
                        'reason': 'replacement_base_not_invalidated',
                        'status': base['status'],
                        'target_status': base['target_status'],
                    })
            if blockers:
                return _blocked(store, requirement_id, blockers)
        try:
            finalized = store.finalize_transition(requirement_id)
        except RuntimeError as exc:
            return _blocked(store, requirement_id, [
                {'reason': 'transition_not_ready', 'detail': str(exc)}])
        return _result(store, requirement_id, finalized['activated'])
    if target == 'paused':
        finalized = _pause_linked(store, board, requirement_id)
        if isinstance(finalized, list):
            return _blocked(store, requirement_id, finalized)
        return _result(store, requirement_id, finalized['activated'])
    if target in {'cancelled', 'invalidated'}:
        finalized = _archive_linked(store, board, requirement_id, target)
        if isinstance(finalized, list):
            return _blocked(store, requirement_id, finalized)
        return _result(store, requirement_id, finalized['activated'])
    raise ValueError(f'unsupported requirement target {target!r}')
