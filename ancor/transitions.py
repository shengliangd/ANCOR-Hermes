"""Native-card convergence before a request transition becomes durable."""
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


def _result(store, request_id, activated=()):
    request = store.get_request(request_id)
    return {
        'request_id': request_id,
        'outcome': 'completed',
        'status': request['status'],
        'target_status': request['target_status'],
        'activated_request_ids': [row['id'] for row in activated],
    }


def _blocked(store, request_id, blockers):
    request = store.get_request(request_id)
    return {
        'request_id': request_id,
        'outcome': 'blocked',
        'status': request['status'],
        'target_status': request['target_status'],
        'blockers': blockers,
        'next_step': 'Resolve the reported card or replacement blockers, then call advance again.',
    }


def _shared_stable_requests(snapshot, request_id, task_id):
    requests = {row['id']: row for row in snapshot['requests']}
    shared = []
    for link in snapshot['task_links']:
        if link['task_id'] != task_id or link['request_id'] == request_id:
            continue
        other = requests.get(link['request_id'])
        if (other is not None and other['status'] in STABLE_STATUSES
                and other.get('target_status') is None):
            shared.append(other['id'])
    return shared


def _archive_linked(store, board, request_id, target):
    snapshot = store.snapshot()
    tasks = {task['id']: task for task in board.list()}
    linked = [link['task_id'] for link in snapshot['task_links']
              if link['request_id'] == request_id]
    replacement_id = None
    if target == 'invalidated':
        for candidate in snapshot['requests']:
            if (candidate['status'] == 'pending'
                    and candidate.get('target_status') == 'active'
                    and request_id in Store._replacement_ids(candidate['replacement_of'])):
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
        shared = _shared_stable_requests(snapshot, request_id, task_id)
        if shared:
            blockers.append({'task_id': task_id,
                             'reason': 'task_shared_with_stable_request',
                             'request_ids': shared})
            continue
        if target == 'invalidated' and replacement_id is not None:
            reason = (f'ANCOR request {request_id} is invalidated by '
                      f'{replacement_id}; archive this superseded card.')
        else:
            reason = (f'ANCOR request {request_id} is cancelled; '
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
    return store.finalize_transition(request_id)


def _pause_linked(store, board, request_id):
    tasks = {task['id']: task for task in board.list()}
    linked = [link['task_id'] for link in store.snapshot()['task_links']
              if link['request_id'] == request_id]
    blockers = []
    for task_id in linked:
        task = tasks.get(task_id)
        if task is None:
            blockers.append({'task_id': task_id, 'reason': 'task_missing'})
            continue
        status = task.get('status')
        if status in {'running', 'ready'}:
            try:
                board.block(task_id, f'ANCOR request {request_id} is paused; '
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
    return store.finalize_transition(request_id)


def advance_request(store, board, request_id):
    """Converge linked native cards, then commit the requested request status."""
    request = store.get_request(request_id)
    target = request.get('target_status')
    if target is None:
        return _result(store, request_id)
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
        try:
            finalized = store.finalize_transition(request_id)
        except RuntimeError as exc:
            return _blocked(store, request_id, [
                {'reason': 'transition_not_ready', 'detail': str(exc)}])
        return _result(store, request_id, finalized['activated'])
    if target == 'paused':
        finalized = _pause_linked(store, board, request_id)
        if isinstance(finalized, list):
            return _blocked(store, request_id, finalized)
        return _result(store, request_id, finalized['activated'])
    if target in {'cancelled', 'invalidated'}:
        finalized = _archive_linked(store, board, request_id, target)
        if isinstance(finalized, list):
            return _blocked(store, request_id, finalized)
        return _result(store, request_id, finalized['activated'])
    raise ValueError(f'unsupported request target {target!r}')
