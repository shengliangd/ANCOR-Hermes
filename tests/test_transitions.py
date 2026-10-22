from copy import deepcopy
from types import SimpleNamespace

import pytest

from ancor.notification import NotificationDispatcher, NotificationOutbox
from ancor.scheduling import Scheduling
from ancor.service import Service
from ancor.store import Store
from ancor.transitions import advance_requirement


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


def requirement(store, title='Requirement'):
    return store.confirm(
        store.create_draft(title, 'Verified result', title=title)['id'], 'human:test')


def test_cancellation_archives_linked_cards_before_committing(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = requirement(store)
    store.link_task(req['id'], 'old-a')
    store.link_task(req['id'], 'old-b')
    board = Board({'old-a': 'running', 'old-b': 'done'})
    observed = []
    board.on_archive = lambda task_id: observed.append(
        (task_id, store.get_requirement(req['id'])['status'],
         store.get_requirement(req['id'])['target_status']))

    requested = store.cancel([req['id']], 'User withdrew it')[0]
    assert requested['status'] == 'active'
    assert requested['target_status'] == 'cancelled'

    result = advance_requirement(store, board, req['id'])
    assert result['outcome'] == 'completed'
    assert observed == [('old-a', 'active', 'cancelled'),
                        ('old-b', 'active', 'cancelled')]
    assert [call[0] for call in board.calls] == ['archive', 'archive']
    assert store.get_requirement(req['id'])['status'] == 'cancelled'
    assert store.get_requirement(req['id'])['target_status'] is None
    assert {row['status'] for row in board.list()} == {'archived'}
    retry = advance_requirement(store, board, req['id'])
    assert retry['outcome'] == 'completed'
    assert len([call for call in board.calls if call[0] == 'archive']) == 2


def test_archive_failure_leaves_cancellation_staged(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = requirement(store)
    store.link_task(req['id'], 'card')
    board = Board({'card': 'running'})

    def fail_archive(task_id, reason):
        raise RuntimeError('native archive unavailable')

    board.archive = fail_archive
    store.cancel([req['id']], 'User withdrew it')
    result = advance_requirement(store, board, req['id'])
    assert result['outcome'] == 'blocked'
    assert result['blockers'][0]['reason'] == 'archive_failed'
    current = store.get_requirement(req['id'])
    assert current['status'] == 'active'
    assert current['target_status'] == 'cancelled'


@pytest.mark.parametrize('mode', ['cancel', 'replace'])
def test_shared_card_blocks_terminal_transition(tmp_path, mode):
    store = Store(tmp_path / 'store.db')
    req = requirement(store, 'First')
    other = requirement(store, 'Second')
    store.link_task(req['id'], 'shared')
    store.link_task(other['id'], 'shared')
    board = Board({'shared': 'running'})

    if mode == 'cancel':
        store.cancel([req['id']], 'First is withdrawn')
    else:
        replacement = store.confirm(store.create_draft(
            'Replacement', 'New result', req['id'], title='Replacement')['id'],
            'human:replacement')
        assert replacement['status'] == 'pending'

    result = advance_requirement(store, board, req['id'])
    assert result['outcome'] == 'blocked'
    assert result['blockers'] == [{
        'task_id': 'shared',
        'reason': 'task_shared_with_stable_requirement',
        'requirement_ids': [other['id']],
    }]
    assert board.calls == []
    current = store.get_requirement(req['id'])
    assert current['status'] == 'active'
    assert current['target_status'] == ('cancelled' if mode == 'cancel' else 'invalidated')
    assert board.list()[0]['status'] == 'running'


def test_replacement_waits_for_every_base_requirement(tmp_path):
    store = Store(tmp_path / 'store.db')
    first = requirement(store, 'First')
    second = requirement(store, 'Second')
    replacement = store.confirm(store.create_draft(
        'Combined replacement', 'Combined result', [first['id'], second['id']],
        title='Combined replacement')['id'], 'human:replacement')
    board = Board({'stable': 'done'})

    blocked = advance_requirement(store, board, replacement['id'])
    assert blocked['outcome'] == 'blocked'
    assert {row['requirement_id'] for row in blocked['blockers']} == {
        first['id'], second['id']}

    first_result = advance_requirement(store, board, first['id'])
    assert first_result['outcome'] == 'completed'
    assert first_result['activated_requirement_ids'] == []
    assert store.get_requirement(replacement['id'])['status'] == 'pending'

    still_blocked = advance_requirement(store, board, replacement['id'])
    assert still_blocked['outcome'] == 'blocked'
    assert {row['requirement_id'] for row in still_blocked['blockers']} == {second['id']}

    final = advance_requirement(store, board, second['id'])
    assert final['outcome'] == 'completed'
    assert final['activated_requirement_ids'] == [replacement['id']]
    assert store.get_requirement(replacement['id'])['status'] == 'active'
    assert store.get_requirement(replacement['id'])['target_status'] is None


def test_pause_blocks_running_and_ready_cards_after_quiescence(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = requirement(store)
    for task_id in ('ready-card', 'running-card', 'todo-card'):
        store.link_task(req['id'], task_id)
    board = Board({'ready-card': 'ready', 'running-card': 'running',
                   'todo-card': 'todo'})
    pause = store.create_requirement_command('pause', req['id'], 'User asked')
    store.confirm_requirement_command(pause['id'], 'human:pause')

    result = advance_requirement(store, board, req['id'])
    assert result['outcome'] == 'completed'
    assert {call[1] for call in board.calls if call[0] == 'block'} == {
        'ready-card', 'running-card'}
    statuses = {row['id']: row['status'] for row in board.list()}
    assert statuses == {'ready-card': 'blocked', 'running-card': 'blocked',
                        'todo-card': 'todo'}
    assert store.get_requirement(req['id'])['status'] == 'paused'


def test_pause_refuses_to_commit_while_a_worker_is_live(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = requirement(store)
    store.link_task(req['id'], 'running-card')
    board = Board({'running-card': 'running'},
                  runs={'running-card': [{'id': 1, 'ended_at': None}]})
    pause = store.create_requirement_command('pause', req['id'], 'User asked')
    store.confirm_requirement_command(pause['id'], 'human:pause')

    blocked = advance_requirement(store, board, req['id'])
    assert blocked['outcome'] == 'blocked'
    assert blocked['blockers'][0]['reason'] == 'worker_quiescence_not_established'
    current = store.get_requirement(req['id'])
    assert current['status'] == 'active' and current['target_status'] == 'paused'

    board.tasks['running-card']['runs'] = []
    completed = advance_requirement(store, board, req['id'])
    assert completed['outcome'] == 'completed'
    assert store.get_requirement(req['id'])['status'] == 'paused'


def test_acceptance_archives_done_cards_and_rejects_nonterminal_cards(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = requirement(store)
    store.link_task(req['id'], 'a-running')
    store.link_task(req['id'], 'z-done')
    board = Board({'a-running': 'running', 'z-done': 'done'})
    service = Service(
        SimpleNamespace(profile_name='ancor-coordinator',
                        get_config=lambda key, default=None: default),
        store, board)
    evidence = {
        'criteria': req['acceptance_criteria'],
        'verification': ['Checked the exact result'],
        'artifacts': ['artifact.txt'],
        'task_ids': ['a-running', 'z-done'],
    }

    with pytest.raises(ValueError, match='must be done'):
        service.acceptance({'action': 'accept', 'requirement_id': req['id'],
                            'evidence': evidence})
    assert store.get_requirement(req['id'])['status'] == 'active'
    assert not [call for call in board.calls if call[0] == 'archive']

    board.tasks['a-running']['task']['status'] = 'done'
    observed = []
    board.on_archive = lambda task_id: observed.append(
        store.get_requirement(req['id'])['status'])
    verified = service.acceptance({'action': 'verify', 'requirement_id': req['id'],
                                   'evidence': evidence})
    assert verified['status'] == 'awaiting_acceptance'
    assert observed == ['active', 'active']
    assert {row['status'] for row in board.list()} == {'archived'}
    accepted = store.accept(req['id'], 'human:accepted')
    assert accepted['status'] == 'accepted'


def test_staged_transition_suppresses_wakes_and_defers_questions(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = requirement(store)
    store.link_task(req['id'], 'blocked-card')
    scheduler = Scheduling(store, lambda: 1000.0)
    wake = scheduler.call({'action': 'schedule', 'requirement_id': req['id'],
                           'reason': 'Recheck later', 'due_at': 2000.0})
    box = NotificationOutbox(str(store.path) + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='human:test')
    box.enqueue('question', 'question', 'Need a decision',
                actor='ancor-coordinator', requirement_id=req['id'],
                task_id='blocked-card')
    dispatcher = NotificationDispatcher(
        box, lambda target, message: {'success': True},
        resolve_requirement=store.get_requirement,
        resolve_task=lambda task_id: {'id': task_id, 'status': 'blocked'})

    pause = store.create_requirement_command('pause', req['id'], 'User asked')
    store.confirm_requirement_command(pause['id'], 'human:pause')

    assert scheduler.pending_wakes() == []
    suppressed = scheduler.call({'action': 'list', 'state': 'suppressed'})['items']
    assert [row['id'] for row in suppressed] == [wake['id']]
    assert dispatcher._gate(box.get('question')) == (
        'defer', 'requirement_transition_pending')

    store.finalize_transition(req['id'])
    assert dispatcher._gate(box.get('question')) == ('defer', 'requirement_paused')
