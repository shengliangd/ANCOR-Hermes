import json
import sqlite3
from types import SimpleNamespace

from ancor.notification import NotificationOutbox
from ancor.scheduling import Scheduling
from ancor.service import Service
from ancor.store import Store
from ancor.views import awaiting_acceptances


def context():
    config = {'coordinator_profile': 'ancor-coordinator',
              'interactor_profile': 'ancor-interactor',
              'human_target': 'test:destination'}
    return SimpleNamespace(profile_name='ancor-interactor',
                           get_config=lambda key, default=None: config.get(key, default))


def verified(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = store.confirm(store.create_draft(
        'Verified brief', 'Verified criteria', title='Human gate')['id'], 'fixture')
    store.link_task(req['id'], 'task')
    store.ack_events([event['id'] for event in store.pending_events()])
    store.submit_acceptance(req['id'], json.dumps({
        'criteria': 'Verified criteria', 'verification': ['checked'],
        'artifacts': ['/result.txt'], 'task_ids': ['task']}))
    box = NotificationOutbox(store.path + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    notice_id = 'accept:' + req['id']
    box.enqueue(notice_id, 'completion', 'Verified result ready for human acceptance.',
                actor='ancor-coordinator', requirement_id=req['id'])
    assert box.claim_next()['id'] == notice_id
    box.mark_sent(notice_id, {'success': True})
    return store, box, req, notice_id


def confirmation_payload(prepared):
    question = prepared['clarify_args']['questions'][0]
    return {'outcome': 'submitted', 'responses': [{
        'question': question['question'], 'choices_offered': question['choices'],
        'status': 'answered', 'user_response': question['choices'][1]}]}


def test_verification_waits_for_human_without_coordinator_attention(tmp_path):
    store, _box, req, _notice = verified(tmp_path)
    assert store.get_requirement(req['id'])['status'] == 'awaiting_acceptance'
    assert store.pending_events() == []
    health = Scheduling(store).health(
        store.snapshot(), [{'id': 'task', 'status': 'done'}], [])
    assert health[0]['state'] == 'awaiting_human_acceptance'
    assert health[0]['reason'] == 'human_acceptance_pending'


def test_human_acceptance_commits_only_after_exact_confirmation(tmp_path):
    store, box, req, _notice = verified(tmp_path)
    service = Service(context(), store)
    prepared = service.requirement({'action': 'accept', 'requirement_id': req['id']})
    args = prepared['clarify_args']
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    assert store.get_requirement(req['id'])['status'] == 'awaiting_acceptance'
    service.post_tool_call('clarify', args, confirmation_payload(prepared),
                           session_id='s', tool_call_id='c')
    assert store.get_requirement(req['id'])['status'] == 'accepted'
    assert store.pending_events() == []
    notice = box.get('accept:' + req['id'])
    assert notice['closed'] is True
    assert notice['close_reason'] == 'accepted_by_human'


def test_human_change_request_returns_to_active_with_feedback_and_wakes(tmp_path):
    store, box, req, _notice = verified(tmp_path)
    service = Service(context(), store)
    prepared = service.requirement({
        'action': 'request_changes', 'requirement_id': req['id'],
        'feedback': 'The chart is unreadable; fix the axis labels.'})
    args = prepared['clarify_args']
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    service.post_tool_call('clarify', args, confirmation_payload(prepared),
                           session_id='s', tool_call_id='c')
    assert store.get_requirement(req['id'])['status'] == 'active'
    pending = store.pending_events()
    assert [event['kind'] for event in pending] == ['requirement.changes_requested']
    assert pending[0]['payload']['feedback'] == 'The chart is unreadable; fix the axis labels.'
    notice = box.get('accept:' + req['id'])
    assert notice['closed'] is True
    assert notice['close_reason'] == 'changes_requested_by_human'


def test_legacy_coordinator_acceptance_reopens_human_gate_idempotently(tmp_path):
    path = tmp_path / 'store.db'
    store = Store(path)
    req = store.confirm(store.create_draft(
        'Legacy brief', 'Legacy criteria', title='Legacy acceptance')['id'], 'legacy')
    with sqlite3.connect(path) as db:
        db.execute("UPDATE requirements SET status='accepted' WHERE id=?", (req['id'],))
    event = store.add_event('requirement.accepted', {
        'requirement_id': req['id'], 'evidence': 'preserved legacy evidence'})
    store.ack_events([row['id'] for row in store.pending_events()])

    migrated = Store(path)
    requirement = migrated.get_requirement(req['id'])
    assert requirement['status'] == 'awaiting_acceptance'
    verification = migrated.latest_verification(req['id'])
    assert verification['payload']['evidence'] == 'preserved legacy evidence'
    assert verification['payload']['migrated_from_event_id'] == event['id']
    assert migrated.pending_events() == []

    reopened = Store(path)
    verifications = [row for row in reopened.acceptances()
                     if row['kind'] == 'requirement.verified']
    assert len(verifications) == 1
    assert reopened.get_requirement(req['id'])['status'] == 'awaiting_acceptance'


def test_current_human_acceptance_is_not_reopened(tmp_path):
    store, _box, req, _notice = verified(tmp_path)
    store.accept(req['id'], 'native:human-accept')
    reopened = Store(store.path)
    assert reopened.get_requirement(req['id'])['status'] == 'accepted'


def test_legacy_completion_without_requirement_column_keeps_human_gate(tmp_path):
    store, box, req, notice_id = verified(tmp_path)
    with sqlite3.connect(store.path + '.notifications.db') as db:
        db.execute('UPDATE human_notifications SET requirement_id=NULL WHERE id=?',
                   (notice_id,))
    rows = NotificationOutbox.read_existing(store.path + '.notifications.db')
    reviews = awaiting_acceptances(store.snapshot()['requirements'], rows)
    assert reviews[0]['id'] == req['id']
    assert reviews[0]['notice_id'] == notice_id
    assert reviews[0]['notice_delivered'] is True

    service = Service(context(), store)
    prepared = service.requirement({'action': 'accept', 'requirement_id': req['id']})
    assert service.pre_tool_call('clarify', prepared['clarify_args'],
                                 session_id='s', tool_call_id='c') is None
    service.post_tool_call('clarify', prepared['clarify_args'],
                           confirmation_payload(prepared),
                           session_id='s', tool_call_id='c')
    assert store.get_requirement(req['id'])['status'] == 'accepted'
    assert box.get(notice_id)['closed'] is True
