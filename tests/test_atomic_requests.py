import json
import sqlite3

import pytest

from ancor.store import Store
from ancor.service import Service
from ancor.notification import NotificationOutbox


def propose(store, rid, ops):
    req = store.get_request(rid) if rid else None
    return store.propose_change(rid, ops, actor='ancor-interactor', source={'input': 'User instruction'}, base=req)


def apply(store, rid, ops):
    return store.commit_change(propose(store, rid, ops)['id'], 'native-confirmed')


@pytest.fixture
def atomic(tmp_path):
    store = Store(tmp_path / 'req.db')
    result = apply(store, None, [{'op': 'create', 'title': 'Report', 'fields': {
        'outcome': ['Deliver a report', 'Include south region'], 'constraint': ['No personal data']}}])
    return store, result['request_id']


def points(store, rid):
    return [p for rows in store.content(rid)['fields'].values() for p in rows]


def test_creation_is_exact_confirmed_idempotent(tmp_path):
    store = Store(tmp_path / 'db')
    p = propose(store, None, [{'op': 'create', 'title': 'Test', 'fields': {'outcome': ['A']}}])
    assert store.snapshot()['requests'] == []
    first = store.commit_change(p['id'], 'confirm')
    assert store.commit_change(p['id'], 'confirm') == first
    assert len(points(store, first['request_id'])) == 1
    assert store.get_change(p['id'])['confirmation_reference'] == 'confirm'


def test_replace_retains_identity_history_and_local_task(atomic):
    store, rid = atomic
    store.link_task(rid, 'running-unrelated-task')
    before = points(store, rid)[0]
    apply(store, rid, [{'op': 'replace', 'version_id': before['version_id'], 'body': 'Deliver Markdown'}])
    after = next(p for p in points(store, rid) if p['point_id'] == before['point_id'])
    assert after['revision'] == 2 and after['replaces'] == before['version_id']
    assert store.get_request(rid)['generation'] == 2
    assert store.get_request(rid)['target_status'] is None
    history = store.content(rid, history=True)['fields']['outcome']
    assert next(p for p in history if p['version_id'] == before['version_id'])['invalidated_at']
    assert store.snapshot()['task_links'] == [{'request_id': rid, 'task_id': 'running-unrelated-task'}]


def test_stale_change_conflicts_without_partial_commit(atomic):
    store, rid = atomic
    old = propose(store, rid, [{'op': 'append', 'field': 'context', 'body': 'old'}])
    apply(store, rid, [{'op': 'append', 'field': 'context', 'body': 'new'}])
    with pytest.raises(RuntimeError, match='conflicts'):
        store.commit_change(old['id'], 'confirmed')
    assert [p['body'] for p in store.content(rid)['fields']['context']] == ['new']
    assert store.get_change(old['id'])['committed_at'] is None


def test_title_and_notes_do_not_conflict_with_content_change(atomic):
    store, rid = atomic
    pending = propose(store, rid, [{'op': 'append', 'field': 'advice', 'body': 'optional'}])
    apply(store, rid, [{'op': 'title', 'title': 'New title'}])
    note = store.add_note(rid, 'first')
    store.update_note(rid, note['id'], 'second')
    store.delete_note(rid, note['id'])
    assert store.get_request(rid)['generation'] == 1
    history = store.note_history(rid, note['id'])['items']
    assert [r['body'] for r in history] == ['first', 'second', 'second']
    assert history[-1]['deleted_at']
    store.commit_change(pending['id'], 'confirm')
    assert store.get_request(rid)['generation'] == 2


def test_failed_batch_is_fully_rolled_back(atomic):
    store, rid = atomic
    original = points(store, rid)
    with pytest.raises((ValueError, RuntimeError)):
        propose(store, rid, [{'op': 'append', 'field': 'context', 'body': 'must not exist'},
                             {'op': 'invalidate', 'version_id': 'missing'}])
    assert points(store, rid) == original


def test_legacy_brief_not_reinterpreted(tmp_path):
    store = Store(tmp_path / 'db')
    old = store.confirm(store.create_draft('A complex ambiguous request')['id'], 'confirmed')
    assert store.content(old['id'])['legacy_brief'] == old['brief']
    assert points(store, old['id']) == []
    apply(store, old['id'], [{'op': 'convert', 'fields': {'outcome': ['Exact confirmed result']}}])
    assert store.content(old['id'])['mode'] == 'points'
    assert store.get_draft(old['draft_id'])['brief'] == 'A complex ambiguous request'


def test_answer_content_and_resolution_are_atomic(atomic):
    store, rid = atomic
    store.create_question(rid, identifier='format', body='Format?', reason='Need output format', actor='coordinator')
    q = store.questions(rid)[0]
    p = propose(store, rid, [{'op': 'append', 'field': 'outcome', 'body': 'Markdown output'},
                           {'op': 'reply', 'question_id': q['id'], 'revision': 1, 'body': 'Markdown'},
                           {'op': 'resolve_question', 'question_id': q['id'], 'revision': 1, 'reason': 'Format recorded'}])
    assert store.questions(rid)[0]['state'] == 'open'
    store.commit_change(p['id'], 'human confirmation')
    q = store.questions(rid)[0]
    assert q['state'] == 'resolved' and q['resolved_change_id'] == p['id']
    assert q['replies'][0]['body'] == 'Markdown'
    assert 'Markdown output' in store.get_request(rid)['brief']


def test_reply_can_leave_question_open(atomic):
    store, rid = atomic
    store.create_question(rid, identifier='q', body='Which?', reason='Need choice', actor='coordinator')
    apply(store, rid, [{'op': 'reply', 'question_id': 'q', 'revision': 1, 'body': 'Not decided'}])
    assert store.questions(rid)[0]['state'] == 'open'
    assert store.get_request(rid)['generation'] == 1


def test_closed_question_rejects_entire_answer_batch(atomic):
    store, rid = atomic
    store.create_question(rid, identifier='q', body='Which?', reason='Need choice', actor='coordinator')
    p = propose(store, rid, [{'op': 'append', 'field': 'context', 'body': 'A'},
                           {'op': 'resolve_question', 'question_id': 'q', 'revision': 1, 'reason': 'A'}])
    store.withdraw_question('q', 'Obsolete', 'coordinator')
    with pytest.raises(RuntimeError):
        store.commit_change(p['id'], 'confirmed')
    assert store.content(rid)['fields']['context'] == []


def test_delivery_intent_recovers_and_is_idempotent(atomic, tmp_path):
    store, rid = atomic
    box = NotificationOutbox(tmp_path / 'notifications', coordinator_profile='coordinator', target='test:user')
    store.create_question(rid, identifier='q', body='Which?', reason='Need choice', actor='coordinator')
    store.sync_questions(box)
    store.sync_questions(box)
    assert len(box.list()) == 1
    apply(store, rid, [{'op': 'resolve_question', 'question_id': 'q', 'revision': 1, 'reason': 'No longer needed'}])
    store.sync_questions(box)
    assert box.get('q')['closed_at']
    assert box.get('q')['answer'] is None


def evidence(store, rid):
    return {'generation': store.get_request(rid)['generation'],
            'verification': ['independent check'], 'artifacts': ['report.md'], 'task_ids': ['t'],
            'coverage': [{'version_id': p['version_id'], 'evidence': 'checked in artifact'}
                         for p in points(store, rid) if p['field'] in {'outcome', 'constraint'}]}


def verify(store, rid):
    store.review_impact(rid, store.get_request(rid)['generation'],
                        [{'task_id': 't', 'decision': 'unaffected', 'reason': 'still valid'}], 'All current outcomes covered')
    store.submit_acceptance(rid, json.dumps(evidence(store, rid)), generation=store.get_request(rid)['generation'])


def test_verification_cannot_use_old_generation(atomic):
    store, rid = atomic
    store.link_task(rid, 't')
    old = evidence(store, rid)
    apply(store, rid, [{'op': 'append', 'field': 'constraint', 'body': 'Offline only'}])
    with pytest.raises(RuntimeError):
        store.submit_acceptance(rid, json.dumps(old), generation=1)


def test_reconciliation_requires_all_tasks_and_survives_ack(atomic):
    store, rid = atomic
    store.link_task(rid, 't')
    with pytest.raises(ValueError):
        store.review_impact(rid, 1, [], 'covered')
    store.review_impact(rid, 1, [{'task_id': 't', 'decision': 'revise', 'reason': 'new constraint'}], 'covered')
    store.ack_events([e['id'] for e in store.pending_events()])
    assert store.pending_coordination()
    with pytest.raises(RuntimeError, match='pending'):
        store.submit_acceptance(rid, json.dumps(evidence(store, rid)), generation=1)
    store.finish_impact(rid, 1, 't', 'changed card')
    assert store.pending_coordination() == []


def test_acceptance_rejects_old_verification_even_after_reverification(atomic):
    store, rid = atomic
    store.link_task(rid, 't')
    verify(store, rid)
    old = propose(store, rid, [{'op': 'accept', 'verification_id': store.latest_verification(rid)['id']}])
    apply(store, rid, [{'op': 'append', 'field': 'context', 'body': 'updated source'}])
    verify(store, rid)
    with pytest.raises(RuntimeError):
        store.commit_change(old['id'], 'confirmed')
    apply(store, rid, [{'op': 'accept', 'verification_id': store.latest_verification(rid)['id']}])
    assert store.get_request(rid)['status'] == 'accepted'


def test_legacy_acceptance_hook_is_also_version_bound(atomic):
    store, rid = atomic
    store.link_task(rid, 't')
    verify(store, rid)
    command = store.create_human_decision(rid, 'accept')
    apply(store, rid, [{'op': 'append', 'field': 'context', 'body': 'new'}])
    verify(store, rid)
    with pytest.raises(RuntimeError, match='stale'):
        store.confirm_human_decision(command['id'], 'confirmed')


def test_rework_preserves_content_generation(atomic):
    store, rid = atomic
    store.link_task(rid, 't')
    verify(store, rid)
    apply(store, rid, [{'op': 'request_changes', 'feedback': 'South region missing'}])
    req = store.get_request(rid)
    assert req['status'] == 'active' and req['generation'] == 1


def test_native_confirmation_commits_exact_change(atomic):
    store, rid = atomic
    service = Service(None, store=store)
    p = propose(store, rid, [{'op': 'append', 'field': 'advice', 'body': 'Optional approach'}])
    args = service.change_presentation(p)['clarify_args']
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    q = args['questions'][0]
    result = {'outcome': 'submitted', 'responses': [{'question': q['question'], 'choices_offered': q['choices'],
              'status': 'answered', 'user_response': q['choices'][1]}]}
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')
    assert store.get_change(p['id'])['committed_at'] is not None
    assert store.content(rid)['fields']['advice'][0]['body'] == 'Optional approach'


def test_missing_or_rejected_confirmation_does_not_commit(atomic):
    store, rid = atomic
    service = Service(None, store=store)
    p = propose(store, rid, [{'op': 'title', 'title': 'Wrong'}])
    args = service.change_presentation(p)['clarify_args']
    service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c')
    service.post_tool_call('clarify', args, {'outcome': 'cancelled'}, session_id='s', tool_call_id='c')
    assert store.get_request(rid)['title'] == 'Report'


def test_legacy_note_migration_marks_missing_history(tmp_path):
    path = tmp_path / 'db'
    store = Store(path)
    req = store.confirm(store.create_draft('Do work')['id'], 'yes')
    with store._transaction() as db:
        db.execute('INSERT INTO request_notes VALUES(?,?,?,?,?,?,NULL)', ('n', req['id'], 'only current', 4, 1, 2))
    store = Store(path)
    history = store.note_history(req['id'], 'n')['items']
    assert len(history) == 1 and history[0]['revision'] == 4 and history[0]['history_missing'] == 1


def test_concurrent_replies_cannot_silently_change_question(atomic):
    store, rid = atomic
    store.create_question(rid, identifier='q', body='Which?', reason='Need choice', actor='coordinator')
    first = propose(store, rid, [{'op': 'reply', 'question_id': 'q', 'revision': 1, 'body': 'A'}])
    second = propose(store, rid, [{'op': 'reply', 'question_id': 'q', 'revision': 1, 'body': 'B'}])
    store.commit_change(first['id'], 'confirm A')
    with pytest.raises(RuntimeError, match='question changed'):
        store.commit_change(second['id'], 'confirm B')
    assert [r['body'] for r in store.questions(rid)[0]['replies']] == ['A']


def test_legacy_answer_after_question_import_is_reconciled(atomic, tmp_path):
    store, rid = atomic
    box = NotificationOutbox(tmp_path / 'outbox', coordinator_profile='coordinator', target='test:user')
    box.enqueue('legacy', 'question', 'Which?', actor='coordinator', request_id=rid)
    store.sync_questions(box)
    box.respond('legacy', 'A', 'legacy-confirmation')
    store.sync_questions(box)
    store.sync_questions(box)
    q = store.questions(rid)[0]
    assert q['state'] == 'resolved' and len(q['replies']) == 1
    assert q['replies'][0]['body'] == 'A'
    assert store.get_request(rid)['generation'] == 1


def test_pause_resume_and_cancel_changes_use_existing_convergence(atomic):
    store, rid = atomic
    apply(store, rid, [{'op': 'pause', 'reason': 'wait'}])
    assert store.get_request(rid)['target_status'] == 'paused'
    store.finalize_transition(rid)
    assert store.get_request(rid)['status'] == 'paused'
    apply(store, rid, [{'op': 'append', 'field': 'context', 'body': 'While paused'}])
    assert store.pending_coordination() == []
    apply(store, rid, [{'op': 'resume', 'reason': 'continue'}])
    store.finalize_transition(rid)
    assert store.get_request(rid)['status'] == 'active'
    assert store.pending_coordination()
    apply(store, rid, [{'op': 'cancel', 'reason': 'withdraw'}])
    store.finalize_transition(rid)
    assert store.get_request(rid)['status'] == 'cancelled'


def test_scoped_grant_is_not_a_request_or_implicit_obligation(atomic):
    store, rid = atomic
    apply(store, rid, [{'op': 'grant', 'action': 'deploy', 'target': 'staging', 'scope': 'delivery 1 only'}])
    grant = store.active_grants(rid)[0]
    assert grant['target'] == 'staging'
    assert store.get_request(rid)['generation'] == 1
    apply(store, rid, [{'op': 'revoke', 'grant_id': grant['id']}])
    assert store.active_grants(rid) == []


def test_host_forwards_first_question_without_existing_outbox(atomic):
    from ancor.host import Host
    from test_host import Context, Board
    store, rid = atomic
    store.create_question(rid, identifier='first', body='Which?', reason='Need input', actor='ancor-coordinator')
    ctx = Context(store.path)
    ctx.config['human_target'] = 'test:user'
    host = Host(ctx, store=store, board=Board())
    host.close_stale_notifications()
    rows = NotificationOutbox.read_existing(store.path + '.notifications.db')
    assert len(rows) == 1 and rows[0]['id'] == 'first'


def test_host_reconstructs_pending_review_after_ack_and_restart(atomic):
    from ancor.host import Host
    from test_host import Context, Board
    store, rid = atomic
    store.ack_events([e['id'] for e in store.pending_events()])
    host = Host(Context(store.path), store=Store(store.path), board=Board())
    events = host.pending(32)
    assert any(e['kind'] == 'request.reconcile_needed' and e['payload']['request_id'] == rid for e in events)
    store.ack_events([e['id'] for e in events])
    assert store.pending_coordination()
    store.review_impact(rid, 1, [], 'No tasks yet; planning is required')
    assert store.pending_coordination() == []


def test_completion_notices_bind_each_verification_attempt(atomic, tmp_path):
    from ancor.views import completion_id
    from ancor.notification import NotificationDispatcher
    store, rid = atomic
    store.link_task(rid, 't')
    box = NotificationOutbox(tmp_path / 'outbox', coordinator_profile='coordinator', target='test:user')
    verify(store, rid)
    first = completion_id(store.get_request(rid))
    box.enqueue(first, 'completion', 'Report ready', actor='coordinator', request_id=rid)
    apply(store, rid, [{'op': 'request_changes', 'feedback': 'Fix missed data'}])
    verify(store, rid)
    second = completion_id(store.get_request(rid))
    assert first != second
    box.enqueue(second, 'completion', 'Report ready', actor='coordinator', request_id=rid)
    dispatcher = NotificationDispatcher(box, None, resolve_request=store.get_request,
                                         resolve_task=lambda _: None)
    assert dispatcher._gate(box.get(first)) == ('close', 'verification_superseded')
    assert dispatcher._gate(box.get(second)) == ('send', None)


def test_new_transition_reason_overrides_legacy_command(atomic):
    store, rid = atomic
    pause = store.create_request_command('pause', rid, 'old reason')
    store.confirm_request_command(pause['id'], 'old reference')
    store.finalize_transition(rid)
    apply(store, rid, [{'op': 'resume', 'reason': 'continue'}])
    store.finalize_transition(rid)
    apply(store, rid, [{'op': 'pause', 'reason': 'new reason'}])
    store.finalize_transition(rid)
    event = store.request_transitions(rid)[-1]
    assert event['payload']['reason'] == 'new reason'
    assert 'change_id' in event['payload']


def test_request_question_waits_without_suspending_independent_work(atomic, tmp_path):
    from ancor.scheduling import Scheduling
    store, rid = atomic
    box = NotificationOutbox(tmp_path / 'notifications', coordinator_profile='coordinator', target='test:user')
    store.create_question(rid, identifier='q', body='Which data?', reason='Need decision', actor='coordinator')
    store.sync_questions(box)
    box.claim_next()
    box.mark_sent('q', {'success': True})
    scheduling = Scheduling(store, clock=lambda: 100)
    state = scheduling.health(store.snapshot(), [], box.list())[0]
    assert state['state'] == 'waiting_on_human'
    store.link_task(rid, 'independent')
    state = scheduling.health(store.snapshot(), [{'id': 'independent', 'status': 'ready'}], box.list())[0]
    assert state['state'] == 'active'
