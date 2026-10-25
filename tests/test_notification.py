import asyncio
import json

import pytest

from ancor.notification import (NativeSender, NotificationDispatcher, NotificationOutbox,
                                TransportUnavailable)


def box(tmp_path, clock=lambda: 0):
    return NotificationOutbox(tmp_path / 'notify.db', coordinator_profile='coordinator',
                              target='telegram:explicit-test-target', clock=clock)


def dispatcher(outbox, sender, *, request_status='active', task_status='blocked',
               clock=lambda: 0):
    return NotificationDispatcher(
        outbox, sender,
        resolve_request=lambda _: {'id': 'req', 'status': request_status},
        resolve_task=lambda _: {'id': 'task', 'status': task_status},
        retry_seconds=15, timeout=1, clock=clock)


def deliver(delivery):
    return asyncio.run(delivery.deliver_once())


def test_durable_enqueue_and_gateway_delivery_then_answer(tmp_path):
    calls = []
    outbox = box(tmp_path)
    row = outbox.enqueue('q1', 'question', 'Which option? Reply with q1.',
                         actor='coordinator', request_id='req', task_id='task')
    assert row['state'] == 'pending'
    reopened = box(tmp_path)
    assert reopened.get('q1')['state'] == 'pending'
    assert deliver(dispatcher(reopened, lambda *args: calls.append(args) or {
        'success': True, 'message_id': 'native-1'}))
    assert reopened.get('q1')['state'] == 'sent'
    assert reopened.get('q1')['receipt']['message_id'] == 'native-1'
    reopened.respond('q1', 'Option B', 'human-session:turn-4')
    persisted = box(tmp_path).get('q1')
    assert persisted['state'] == 'sent'
    assert persisted['answer'] == 'Option B'
    assert persisted['answer_reference'] == 'human-session:turn-4'
    assert calls == [('telegram:explicit-test-target', 'Which option? Reply with q1.')]


def test_worker_cannot_select_or_withdraw_questions(tmp_path):
    outbox = box(tmp_path)
    with pytest.raises(PermissionError):
        outbox.enqueue('w', 'question', 'raw worker event', actor='worker')
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    with pytest.raises(PermissionError):
        outbox.withdraw('q', actor='worker', reason='not allowed')


def test_idempotency_and_no_duplicate_delivery(tmp_path):
    calls = []
    outbox = box(tmp_path)
    for _ in range(2):
        outbox.enqueue('c', 'completion', 'Done', actor='coordinator',
                       request_id='req')
    delivery = dispatcher(outbox, lambda *args: calls.append(args) or {'success': True},
                          request_status='awaiting_acceptance')
    assert deliver(delivery) is True
    assert deliver(delivery) is False
    assert len(calls) == 1
    with pytest.raises(ValueError):
        outbox.enqueue('c', 'completion', 'Changed', actor='coordinator',
                       request_id='req')


def test_identical_question_cannot_be_enqueued_under_a_new_id(tmp_path):
    outbox = box(tmp_path)
    first = outbox.enqueue('q1', 'question', 'Which option?', actor='coordinator',
                           request_id='req', task_id='task')
    assert outbox.enqueue('q1', 'question', 'Which option?', actor='coordinator',
                          request_id='req', task_id='task')['id'] == first['id']
    with pytest.raises(ValueError, match='identical notification.*q1'):
        outbox.enqueue('q1-retry', 'question', 'Which option?', actor='coordinator',
                       request_id='req', task_id='task')
    outbox.withdraw('q1', actor='coordinator', reason='no longer blocked')
    with pytest.raises(ValueError, match='identical notification.*closed'):
        outbox.enqueue('q1-closed-retry', 'question', 'Which option?', actor='coordinator',
                       request_id='req', task_id='task')
    other = outbox.enqueue('q2', 'question', 'Which option?', actor='coordinator',
                           request_id='other', task_id='task')
    assert other['id'] == 'q2'


def test_completion_close_is_idempotent_and_completion_only(tmp_path):
    outbox = box(tmp_path)
    outbox.enqueue('c', 'completion', 'Done', actor='coordinator',
                   request_id='req')
    assert outbox.claim_next()['id'] == 'c'
    outbox.mark_sent('c', {'success': True})
    first = outbox.close_completions(
        'req', closed_by='interactor', reason='accepted_by_human')
    assert len(first) == 1 and first[0]['closed'] is True
    assert first[0]['closed_by'] == 'interactor'
    assert first[0]['close_reason'] == 'accepted_by_human'
    again = outbox.close_completions(
        'req', closed_by='interactor', reason='accepted_by_human')
    assert again == []
    outbox.enqueue('q', 'question', 'Which label?', actor='coordinator',
                   request_id='req', task_id='task')
    assert outbox.close_completions(
        'req', closed_by='interactor', reason='accepted_by_human') == []


def test_multiple_open_questions_allowed_and_withdraw_closes_one(tmp_path):
    outbox = box(tmp_path)
    outbox.enqueue('q1', 'question', 'Which label?', actor='coordinator',
                   request_id='req', task_id='task')
    outbox.enqueue('q2', 'question', 'Which owner?', actor='coordinator',
                   request_id='req', task_id='task')
    assert [row['id'] for row in outbox.list()] == ['q1', 'q2']
    closed = outbox.withdraw('q1', actor='coordinator', reason='bypassed by new input')
    assert closed['closed'] is True
    assert closed['answer'] is None
    assert closed['close_reason'] == 'bypassed by new input'
    assert outbox.get('q2')['closed'] is False
    with pytest.raises(ValueError, match='closed'):
        outbox.respond('q1', 'late answer', 'human:late')


def test_answer_before_delivery_is_not_sent(tmp_path):
    calls = []
    outbox = box(tmp_path)
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    answered = outbox.respond('q', 'early answer', 'human:early')
    assert answered['state'] == 'answered'
    delivery = dispatcher(outbox, lambda *args: calls.append(args) or {'success': True})
    assert deliver(delivery) is False
    assert calls == []


def test_answer_after_claim_is_not_sent(tmp_path):
    calls = []
    outbox = box(tmp_path)
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    claim = outbox.claim_next

    def claim_then_answer():
        row = claim()
        if row is not None:
            outbox.respond(row['id'], 'answer won the race', 'human:race')
        return row

    outbox.claim_next = claim_then_answer
    delivery = dispatcher(outbox, lambda *args: calls.append(args) or {'success': True})
    assert deliver(delivery) is True
    assert calls == []
    row = outbox.get('q')
    assert row['state'] == 'answered'
    assert row['answer'] == 'answer won the race'


def test_close_after_claim_is_not_sent(tmp_path):
    calls = []
    outbox = box(tmp_path)
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    claim = outbox.claim_next

    def claim_then_withdraw():
        row = claim()
        if row is not None:
            outbox.withdraw(row['id'], actor='coordinator', reason='bypassed by new input')
        return row

    outbox.claim_next = claim_then_withdraw
    delivery = dispatcher(outbox, lambda *args: calls.append(args) or {'success': True})
    assert deliver(delivery) is True
    assert calls == []
    row = outbox.get('q')
    assert row['state'] == 'closed'
    assert row['closed'] is True
    assert row['answer'] is None


@pytest.mark.parametrize('receipt', [
    {'success': True, 'skipped': True}, {'accepted': True}, {'error': 'no'}, None])
def test_unconfirmed_receipts_never_become_sent(tmp_path, receipt):
    calls = []
    outbox = box(tmp_path)
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    delivery = dispatcher(outbox, lambda *args: calls.append(args) or receipt)
    assert deliver(delivery) is True
    assert outbox.get('q')['state'] == 'uncertain'
    assert deliver(delivery) is False
    assert len(calls) == 1


def test_transport_unavailable_returns_pending_with_backoff(tmp_path):
    now = [100.0]
    outbox = box(tmp_path, clock=lambda: now[0])
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    def unavailable(*_):
        raise TransportUnavailable('transport_unavailable')
    delivery = dispatcher(outbox, unavailable, clock=lambda: now[0])
    assert deliver(delivery) is True
    row = outbox.get('q')
    assert row['state'] == 'pending'
    assert row['next_attempt_at'] == 115
    assert json.loads(row['error'])['reason_code'] == 'transport_unavailable'
    assert deliver(delivery) is False
    now[0] = 115
    assert deliver(delivery) is True
    assert outbox.get('q')['state'] == 'pending'


def test_timeout_is_uncertain_and_not_retried(tmp_path):
    outbox = box(tmp_path)
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    def slow(*_):
        import time
        time.sleep(1.2)
        return {'success': True}
    delivery = dispatcher(outbox, slow)
    assert deliver(delivery) is True
    row = outbox.get('q')
    assert row['state'] == 'uncertain'
    assert json.loads(row['error'])['reason_code'] == 'timeout'
    assert deliver(delivery) is False


def test_crash_claim_survives_restart_without_resend(tmp_path):
    outbox = box(tmp_path)
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    claimed = outbox.claim_next()
    assert claimed['state'] == 'sending'
    assert box(tmp_path).claim_next() is None
    assert box(tmp_path).get('q')['state'] == 'sending'


def test_gate_closes_terminal_work_without_sending(tmp_path):
    calls = []
    outbox = box(tmp_path)
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    delivery = dispatcher(outbox, lambda *args: calls.append(args) or {'success': True},
                          request_status='cancelled')
    assert deliver(delivery) is True
    row = outbox.get('q')
    assert row['closed'] is True
    assert row['close_reason'] == 'request_cancelled'
    assert row['answer'] is None
    assert calls == []


def test_completion_gate_closes_after_human_change_request(tmp_path):
    calls = []
    outbox = box(tmp_path)
    outbox.enqueue('c', 'completion', 'Verified result', actor='coordinator',
                   request_id='req')
    delivery = dispatcher(outbox, lambda *args: calls.append(args) or {'success': True},
                          request_status='active')
    assert deliver(delivery) is True
    row = outbox.get('c')
    assert row['closed'] is True
    assert row['close_reason'] == 'completion_no_longer_pending'
    assert calls == []


def test_text_only_and_explicit_target(tmp_path):
    with pytest.raises(ValueError):
        NotificationOutbox(tmp_path / 'x', coordinator_profile='coordinator', target='telegram')
    with pytest.raises(ValueError):
        box(tmp_path).enqueue('q', 'question', 'MEDIA:/secret', actor='coordinator')
    with pytest.raises(ValueError, match='human_target'):
        NotificationOutbox(tmp_path / 'y', coordinator_profile='coordinator').enqueue(
            'q', 'question', 'Question', actor='coordinator')


def test_answer_is_immutable_and_not_completion_acceptance(tmp_path):
    outbox = box(tmp_path)
    outbox.enqueue('q', 'question', 'Question', actor='coordinator',
                   request_id='req', task_id='task')
    outbox.respond('q', 'yes', 'turn-1')
    outbox.respond('q', 'yes', 'turn-1')
    with pytest.raises(ValueError):
        outbox.respond('q', 'no', 'turn-2')
    outbox.enqueue('c', 'completion', 'Complete', actor='coordinator',
                   request_id='req')
    with pytest.raises(ValueError):
        outbox.respond('c', 'accepted', 'turn-3')


def test_native_sender_requires_explicit_profile():
    with pytest.raises(ValueError):
        NativeSender('')
