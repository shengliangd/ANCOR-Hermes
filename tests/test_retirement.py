import importlib
import importlib.util
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ancor.store import Store


class ReadOnlyBoard:
    def __init__(self):
        self.detail = {'task': {'id': 't1', 'status': 'blocked'}, 'runs': [],
                       'parents': ['previous'], 'children': ['replacement']}
        self.calls = []

    def _call(self, *args):
        self.calls.append(args)
        assert args == ('show', 't1'), 'retirement must not mutate native state'
        return deepcopy(self.detail)


@pytest.fixture
def case(tmp_path):
    store = Store(tmp_path / 'requirements.db')
    req = store.confirm(store.create_draft('Goal', 'Criteria')['id'], 'human')
    store.link_task(req['id'], 't1')
    ctx = SimpleNamespace(profile_name='ancor-coordinator', get_config=lambda key, default=None: default)
    return ctx, store, ReadOnlyBoard(), req


def retirement(case):
    assert importlib.util.find_spec('ancor.retirement'), 'explicit retirement is missing'
    return importlib.import_module('ancor.retirement').Retirement(*case[:3])


def test_retirement_is_durable_disposition_not_fake_completion(case):
    ctx, store, board, req = case
    before = store.snapshot()
    native_before = deepcopy(board.detail)
    result = retirement(case).retire(req['id'], 't1', 'Alternative abandoned')
    assert result == {'requirement_id': req['id'], 'task_id': 't1', 'reason': 'Alternative abandoned'}
    assert retirement(case).effective_retired(req['id']) == {'t1': result}
    assert store.snapshot() == before
    assert board.detail == native_before
    assert retirement(case).retire(req['id'], 't1', 'Alternative abandoned') == result
    with pytest.raises(ValueError, match='already retired'):
        retirement(case).retire(req['id'], 't1', 'Rewrite history')


@pytest.mark.parametrize('revival', ['ready', 'running', 'triage', 'live_run', 'live_pid', 'dead_pid_live_run'])
def test_acceptance_rechecks_retirement_every_time(case, monkeypatch, revival):
    import os
    op = retirement(case)
    req = case[3]
    op.retire(req['id'], 't1', 'Abandoned')
    assert 't1' in op.effective_retired(req['id'])
    if revival in {'ready', 'running', 'triage'}:
        case[2].detail['task']['status'] = revival
    else:
        case[2].detail['runs'] = [{'ended_at': None if revival != 'live_pid' else 'ended',
                                 'worker_pid': os.getpid() if revival != 'live_run' else None}]
        if revival == 'dead_pid_live_run':
            def absent(*args):
                raise ProcessLookupError()
            monkeypatch.setattr(os, 'kill', absent)
    assert op.effective_retired(req['id']) == {}


@pytest.mark.parametrize('status', ['blocked', 'done'])
def test_invalidated_requirements_can_retire_without_deleting_links(case, status):
    ctx, store, board, req = case
    store.confirm(store.create_draft('Replacement', 'New criteria', req['id'])['id'], 'human-2')
    board.detail['task']['status'] = status
    result = retirement(case).retire(req['id'], 't1', 'Superseded')
    assert retirement(case).effective_retired(req['id']) == {'t1': result}
    assert store.snapshot()['task_links'] == [{'requirement_id': req['id'], 'task_id': 't1'}]


@pytest.mark.parametrize('fault', ['ready', 'running', 'todo', 'review', 'triage', 'cancelled',
                                   'live_run', 'live_pid', 'unlinked', 'accepted', 'worker', 'blank'])
def test_refuses_unsafe_retirement(case, fault):
    import os
    ctx, store, board, req = case
    if fault in {'ready', 'running', 'todo', 'review', 'triage', 'cancelled'}:
        board.detail['task']['status'] = fault
    elif fault == 'live_run':
        board.detail['runs'] = [{'ended_at': None, 'worker_pid': None}]
    elif fault == 'live_pid':
        board.detail['runs'] = [{'ended_at': 'yesterday', 'worker_pid': os.getpid()}]
    elif fault == 'accepted':
        store.accept(req['id'], 'Evidence')
    elif fault == 'worker':
        ctx.profile_name = 'ancor-worker'
    op = retirement(case)
    with pytest.raises((ValueError, RuntimeError, PermissionError)):
        op.retire(req['id'], 'foreign' if fault == 'unlinked' else 't1',
                  '  ' if fault == 'blank' else 'Abandoned')
    assert op.effective_retired(req['id']) == {}
