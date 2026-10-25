import json
import sqlite3
from types import SimpleNamespace

from ancor.notification import NotificationOutbox
from ancor.scheduling import Scheduling
from ancor.service import Service
from ancor.store import Store


def context():
    config = {'coordinator_profile': 'ancor-coordinator',
              'interactor_profile': 'ancor-interactor',
              'human_target': 'test:destination'}
    return SimpleNamespace(profile_name='ancor-interactor',
                           get_config=lambda key, default=None: config.get(key, default))


def verified(tmp_path):
    store = Store(tmp_path / 'store.db')
    req = store.confirm(store.create_draft(
        'Verified brief', title='Human gate')['id'], 'fixture')
    store.link_task(req['id'], 'task')
    store.ack_events([event['id'] for event in store.pending_events()])
    store.submit_acceptance(req['id'], json.dumps({
        'brief': 'Verified brief', 'verification': ['checked'],
        'artifacts': ['/result.txt'], 'task_ids': ['task']}))
    box = NotificationOutbox(store.path + '.notifications.db',
                             coordinator_profile='ancor-coordinator',
                             target='test:destination')
    notice_id = 'accept:' + req['id']
    box.enqueue(notice_id, 'completion', 'Verified result ready for human acceptance.',
                actor='ancor-coordinator', request_id=req['id'])
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
    assert store.get_request(req['id'])['status'] == 'awaiting_acceptance'
    assert store.pending_events() == []
    health = Scheduling(store).health(
        store.snapshot(), [{'id': 'task', 'status': 'done'}], [])
    assert health[0]['state'] == 'awaiting_human_acceptance'
    assert health[0]['reason'] == 'human_acceptance_pending'


def test_human_acceptance_commits_only_after_exact_confirmation(tmp_path):
    store, box, req, _notice = verified(tmp_path)
    service = Service(context(), store)
    prepared = service.request({'action': 'accept', 'request_id': req['id']})
    args = prepared['clarify_args']
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    assert store.get_request(req['id'])['status'] == 'awaiting_acceptance'
    service.post_tool_call('clarify', args, confirmation_payload(prepared),
                           session_id='s', tool_call_id='c')
    assert store.get_request(req['id'])['status'] == 'accepted'
    assert store.pending_events() == []
    notice = box.get('accept:' + req['id'])
    assert notice['closed'] is True
    assert notice['close_reason'] == 'accepted_by_human'


def test_human_change_request_returns_to_active_with_feedback_and_wakes(tmp_path):
    store, box, req, _notice = verified(tmp_path)
    service = Service(context(), store)
    prepared = service.request({
        'action': 'request_changes', 'request_id': req['id'],
        'feedback': 'The chart is unreadable; fix the axis labels.'})
    args = prepared['clarify_args']
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    service.post_tool_call('clarify', args, confirmation_payload(prepared),
                           session_id='s', tool_call_id='c')
    assert store.get_request(req['id'])['status'] == 'active'
    pending = store.pending_events()
    assert [event['kind'] for event in pending] == ['request.changes_requested']
    assert pending[0]['payload']['feedback'] == 'The chart is unreadable; fix the axis labels.'
    notice = box.get('accept:' + req['id'])
    assert notice['closed'] is True
    assert notice['close_reason'] == 'changes_requested_by_human'


def test_legacy_coordinator_acceptance_reopens_human_gate_idempotently(tmp_path):
    path = tmp_path / 'store.db'
    store = Store(path)
    req = store.confirm(store.create_draft(
        'Legacy brief', title='Legacy acceptance')['id'], 'legacy')
    with sqlite3.connect(path) as db:
        db.execute("UPDATE requests SET status='accepted' WHERE id=?", (req['id'],))
    event = store.add_event('request.accepted', {
        'request_id': req['id'], 'evidence': 'preserved legacy evidence'})
    store.ack_events([row['id'] for row in store.pending_events()])

    migrated = Store(path)
    request = migrated.get_request(req['id'])
    assert request['status'] == 'awaiting_acceptance'
    verification = migrated.latest_verification(req['id'])
    assert verification['payload']['evidence'] == 'preserved legacy evidence'
    assert verification['payload']['migrated_from_event_id'] == event['id']
    assert migrated.pending_events() == []

    reopened = Store(path)
    verifications = [row for row in reopened.acceptances()
                     if row['kind'] == 'request.verified']
    assert len(verifications) == 1
    assert reopened.get_request(req['id'])['status'] == 'awaiting_acceptance'


def test_current_human_acceptance_is_not_reopened(tmp_path):
    store, _box, req, _notice = verified(tmp_path)
    store.accept(req['id'], 'native:human-accept')
    reopened = Store(store.path)
    assert reopened.get_request(req['id'])['status'] == 'accepted'


def test_completed_cards_do_not_offer_human_acceptance():
    from ancor.views import acceptance_ref
    status = acceptance_ref({'status': 'active', 'generation': 3}, [{'status': 'done'}])
    assert status['status'] == 'pending_verification'
    assert status['human_acceptance_available'] is False
    assert status['waiting_for'] == 'coordinator_verification'
    status = acceptance_ref({'status': 'active', 'generation': 3}, [{'status': 'archived'}],
                            {'kind': 'request.verified', 'payload': {'generation': 2}})
    assert status['status'] == 'verification_outdated'
    assert status['human_acceptance_available'] is False
