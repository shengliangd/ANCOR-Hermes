from copy import deepcopy
from types import SimpleNamespace

import pytest

from ancor.notification import NotificationDispatcher, NotificationOutbox
from ancor.scheduling import Scheduling
from ancor.service import Service
from ancor.store import Store
from ancor.transitions import advance_request


class Board:
    def __init__(self, statuses, *, runs=None):
        self.tasks = {
            task_id: {'task': {'id': task_id, 'status': status},
                      'runs': list((runs or {}).get(task_id, []))}
            for task_id, status in statuses.items()
        }
        self.calls = []
        self.on_archive = None

    def list(self):
        return [deepcopy(row['task']) for row in self.tasks.values()]

    def _call(self, action, task_id):
        assert action == 'show'
        return deepcopy(self.tasks[task_id])

    def block(self, task_id, reason):
        self.calls.append(('block', task_id, reason))
        self.tasks[task_id]['task']['status'] = 'blocked'
        return deepcopy(self.tasks[task_id]['task'])

    def archive(self, task_id, reason):
        if self.on_archive is not None:
            self.on_archive(task_id)
        self.calls.append(('archive', task_id, reason))
        self.tasks[task_id]['task']['status'] = 'archived'
        return deepcopy(self.tasks[task_id]['task'])


def request(store, title='Request'):
    return store.confirm(
        store.create_draft(title, title=title)['id'], 'human:test')


def test_cancellation_archives_linked_cards_before_committing(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = request(store)
    store.link_task(req['id'], 'old-a')
    store.link_task(req['id'], 'old-b')
    board = Board({'old-a': 'running', 'old-b': 'done'})
    observed = []
    board.on_archive = lambda task_id: observed.append(
        (task_id, store.get_request(req['id'])['status'],
         store.get_request(req['id'])['target_status']))

    requested = store.cancel([req['id']], 'User withdrew it')[0]
    assert requested['status'] == 'active'
    assert requested['target_status'] == 'cancelled'

    result = advance_request(store, board, req['id'])
    assert result['outcome'] == 'completed'
    assert observed == [('old-a', 'active', 'cancelled'),
                        ('old-b', 'active', 'cancelled')]
    assert [call[0] for call in board.calls] == ['archive', 'archive']
    assert store.get_request(req['id'])['status'] == 'cancelled'
    assert store.get_request(req['id'])['target_status'] is None
    assert {row['status'] for row in board.list()} == {'archived'}
    retry = advance_request(store, board, req['id'])
    assert retry['outcome'] == 'completed'
    assert len([call for call in board.calls if call[0] == 'archive']) == 2


def test_archive_failure_leaves_cancellation_staged(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = request(store)
    store.link_task(req['id'], 'card')
    board = Board({'card': 'running'})

    def fail_archive(task_id, reason):
        raise RuntimeError('native archive unavailable')

    board.archive = fail_archive
    store.cancel([req['id']], 'User withdrew it')
    result = advance_request(store, board, req['id'])
    assert result['outcome'] == 'blocked'
    assert result['blockers'][0]['reason'] == 'archive_failed'
    current = store.get_request(req['id'])
    assert current['status'] == 'active'
    assert current['target_status'] == 'cancelled'


def test_cross_request_card_association_is_rejected(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = request(store, 'First')
    other = request(store, 'Second')
    store.link_task(req['id'], 'owned')
    with pytest.raises(ValueError, match='task ownership conflict'):
        store.link_task(other['id'], 'owned')
    assert store.snapshot()['task_links'] == [{'request_id': req['id'], 'task_id': 'owned'}]


def test_replacement_waits_for_every_base_request(tmp_path):
    store = Store(tmp_path / 'store.db')
    first = request(store, 'First')
    second = request(store, 'Second')
    replacement = store.confirm(store.create_draft(
        'Combined replacement', [first['id'], second['id']],
        title='Combined replacement')['id'], 'human:replacement')
    board = Board({'stable': 'done'})

    blocked = advance_request(store, board, replacement['id'])
    assert blocked['outcome'] == 'blocked'
    assert {row['request_id'] for row in blocked['blockers']} == {
        first['id'], second['id']}

    first_result = advance_request(store, board, first['id'])
    assert first_result['outcome'] == 'completed'
    assert first_result['activated_request_ids'] == []
    assert store.get_request(replacement['id'])['status'] == 'pending'

    still_blocked = advance_request(store, board, replacement['id'])
    assert still_blocked['outcome'] == 'blocked'
    assert {row['request_id'] for row in still_blocked['blockers']} == {second['id']}

    final = advance_request(store, board, second['id'])
    assert final['outcome'] == 'completed'
    assert final['activated_request_ids'] == [replacement['id']]
    assert store.get_request(replacement['id'])['status'] == 'active'
    assert store.get_request(replacement['id'])['target_status'] is None


def test_pause_blocks_running_and_ready_cards_after_quiescence(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = request(store)
    for task_id in ('ready-card', 'running-card', 'todo-card'):
        store.link_task(req['id'], task_id)
    board = Board({'ready-card': 'ready', 'running-card': 'running',
                   'todo-card': 'todo'})
    pause = store.create_request_command('pause', req['id'], 'User asked')
    store.confirm_request_command(pause['id'], 'human:pause')

    result = advance_request(store, board, req['id'])
    assert result['outcome'] == 'completed'
    assert {call[1] for call in board.calls if call[0] == 'block'} == {
        'ready-card', 'running-card'}
    statuses = {row['id']: row['status'] for row in board.list()}
    assert statuses == {'ready-card': 'blocked', 'running-card': 'blocked',
                        'todo-card': 'todo'}
    assert store.get_request(req['id'])['status'] == 'paused'


def test_pause_refuses_to_commit_while_a_worker_is_live(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = request(store)
    store.link_task(req['id'], 'running-card')
    board = Board({'running-card': 'running'},
                  runs={'running-card': [{'id': 1, 'ended_at': None}]})
    pause = store.create_request_command('pause', req['id'], 'User asked')
    store.confirm_request_command(pause['id'], 'human:pause')

    blocked = advance_request(store, board, req['id'])
    assert blocked['outcome'] == 'blocked'
    assert blocked['blockers'][0]['reason'] == 'worker_quiescence_not_established'
    current = store.get_request(req['id'])
    assert current['status'] == 'active' and current['target_status'] == 'paused'

    board.tasks['running-card']['runs'] = []
    completed = advance_request(store, board, req['id'])
    assert completed['outcome'] == 'completed'
    assert store.get_request(req['id'])['status'] == 'paused'


def test_acceptance_archives_done_cards_and_rejects_nonterminal_cards(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = request(store)
    store.link_task(req['id'], 'a-running')
    store.link_task(req['id'], 'z-done')
    board = Board({'a-running': 'running', 'z-done': 'done'})
    service = Service(
        SimpleNamespace(profile_name='ancor-coordinator',
                        get_config=lambda key, default=None: default),
        store, board)
    evidence = {
        'brief': req['brief'],
        'verification': ['Checked the exact result'],
        'artifacts': ['artifact.txt'],
        'task_ids': ['a-running', 'z-done'],
    }

    with pytest.raises(ValueError, match='must be done'):
        service.acceptance({'action': 'accept', 'request_id': req['id'],
                            'evidence': evidence})
    assert store.get_request(req['id'])['status'] == 'active'
    assert not [call for call in board.calls if call[0] == 'archive']

    board.tasks['a-running']['task']['status'] = 'done'
    observed = []
    board.on_archive = lambda task_id: observed.append(
        store.get_request(req['id'])['status'])
    verified = service.acceptance({'action': 'verify', 'request_id': req['id'],
                                   'evidence': evidence})
    assert verified['status'] == 'awaiting_acceptance'
    assert observed == ['active', 'active']
    assert {row['status'] for row in board.list()} == {'archived'}
    accepted = store.accept(req['id'], 'human:accepted')
    assert accepted['status'] == 'accepted'


def test_acceptance_requires_the_exact_stored_brief(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = request(store)
    store.link_task(req['id'], 'done-card')
    board = Board({'done-card': 'done'})
    service = Service(
        SimpleNamespace(profile_name='ancor-coordinator',
                        get_config=lambda key, default=None: default),
        store, board)
    evidence = {
        'brief': req['brief'] + ' (paraphrased)',
        'verification': ['Checked the exact result'],
        'artifacts': ['artifact.txt'],
        'task_ids': ['done-card'],
    }

    with pytest.raises(ValueError, match='exact stored brief'):
        service.acceptance({'action': 'verify', 'request_id': req['id'],
                            'evidence': evidence})
    assert store.get_request(req['id'])['status'] == 'active'
    assert not [call for call in board.calls if call[0] == 'archive']

    evidence['brief'] = req['brief']
    verified = service.acceptance({'action': 'verify', 'request_id': req['id'],
                                   'evidence': evidence})
    assert verified['status'] == 'awaiting_acceptance'


def test_staged_transition_suppresses_wakes_and_defers_questions(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = request(store)
    store.link_task(req['id'], 'blocked-card')
    scheduler = Scheduling(store, lambda: 1000.0)
    wake = scheduler.call({'action': 'schedule', 'request_id': req['id'],
                           'reason': 'Recheck later', 'due_at': 2000.0})
    box = NotificationOutbox(str(store.path) + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='human:test')
    box.enqueue('question', 'question', 'Need a decision',
                actor='ancor-coordinator', request_id=req['id'],
                task_id='blocked-card')
    dispatcher = NotificationDispatcher(
        box, lambda target, message: {'success': True},
        resolve_request=store.get_request,
        resolve_task=lambda task_id: {'id': task_id, 'status': 'blocked'})

    pause = store.create_request_command('pause', req['id'], 'User asked')
    store.confirm_request_command(pause['id'], 'human:pause')

    assert scheduler.pending_wakes() == []
    suppressed = scheduler.call({'action': 'list', 'state': 'suppressed'})['items']
    assert [row['id'] for row in suppressed] == [wake['id']]
    assert dispatcher._gate(box.get('question')) == (
        'defer', 'request_transition_pending')

    store.finalize_transition(req['id'])
    assert dispatcher._gate(box.get('question')) == ('defer', 'request_paused')
