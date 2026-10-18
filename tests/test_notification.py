import json
from types import SimpleNamespace

import pytest

from ancor.notification import HermesSender, NotificationOutbox


def box(tmp_path, sender=lambda *_: {'success': True, 'message_id': 'native-1'}):
    return NotificationOutbox(tmp_path / 'notify.db', leader_profile='leader',
                              target='telegram:explicit-test-target', sender=sender)


def test_durable_selection_delivery_and_answer_readback(tmp_path):
    first = box(tmp_path)
    first.enqueue('q1', 'question', 'Which option? Reply with q1.', actor='leader')
    reopened = box(tmp_path)
    assert reopened.get('q1')['state'] == 'pending'
    assert reopened.deliver('q1', actor='leader')['receipt']['message_id'] == 'native-1'
    reopened.respond('q1', 'Option B', 'human-session:turn-4')
    persisted = box(tmp_path).get('q1')
    assert persisted['state'] == 'sent'
    assert persisted['answer'] == 'Option B'
    assert persisted['answer_reference'] == 'human-session:turn-4'
    assert box(tmp_path).list() == [persisted]


def test_worker_cannot_select_or_deliver(tmp_path):
    outbox = box(tmp_path)
    with pytest.raises(PermissionError):
        outbox.enqueue('w', 'question', 'raw worker event', actor='worker')
    outbox.enqueue('c', 'completion', 'Leader verified completion', actor='leader')
    with pytest.raises(PermissionError):
        outbox.deliver('c', actor='worker')
    assert outbox.get('c')['state'] == 'pending'


def test_idempotency_and_no_automatic_duplicate_send(tmp_path):
    calls = []
    outbox = box(tmp_path, lambda *args: calls.append(args) or {'success': True})
    for _ in range(2):
        outbox.enqueue('c', 'completion', 'Done', actor='leader')
        outbox.deliver('c', actor='leader')
    assert len(calls) == 1
    with pytest.raises(ValueError):
        outbox.enqueue('c', 'completion', 'Changed', actor='leader')


@pytest.mark.parametrize('receipt', [{'success': True, 'skipped': True}, {'accepted': True}, {'error': 'no'}, None])
def test_unconfirmed_receipts_never_become_sent(tmp_path, receipt):
    outbox = box(tmp_path, lambda *_: receipt)
    outbox.enqueue('q', 'question', 'Question', actor='leader')
    assert outbox.deliver('q', actor='leader')['state'] == 'uncertain'
    assert box(tmp_path).deliver('q', actor='leader')['state'] == 'uncertain'


def test_crash_claim_survives_restart_without_resend(tmp_path):
    def crash(*_):
        raise KeyboardInterrupt()
    outbox = box(tmp_path, crash)
    outbox.enqueue('q', 'question', 'Question', actor='leader')
    with pytest.raises(KeyboardInterrupt):
        outbox.deliver('q', actor='leader')
    assert box(tmp_path).deliver('q', actor='leader')['state'] == 'sending'


def test_public_cli_argv_and_receipt(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({'success': True, 'message_id': '42'}))
    monkeypatch.setattr('ancor.notification.subprocess.run', run)
    assert HermesSender('transport-owner')('telegram:123', 'Hello')['message_id'] == '42'
    argv, kwargs = calls[0]
    assert argv == ['hermes', '--profile', 'transport-owner', 'send', '--to', 'telegram:123', '--file', '-', '--json']
    assert kwargs['input'] == 'Hello'
    assert 'env' not in kwargs  # native profile resolution; no credential copying


@pytest.mark.parametrize('stdout,code', [('garbage', 0), ('[]', 0), ('{"success":true,"skipped":true}', 0), ('{"success":true}', 1)])
def test_cli_rejects_invalid_or_skipped_delivery(monkeypatch, stdout, code):
    monkeypatch.setattr('ancor.notification.subprocess.run', lambda *a, **k: SimpleNamespace(returncode=code, stdout=stdout))
    with pytest.raises(RuntimeError):
        HermesSender('transport')('telegram:123', 'Hello')


def test_text_only_and_explicit_target(tmp_path):
    with pytest.raises(ValueError):
        NotificationOutbox(tmp_path / 'x', leader_profile='leader', target='telegram', sender=None)
    with pytest.raises(ValueError):
        box(tmp_path).enqueue('q', 'question', 'MEDIA:/secret', actor='leader')


def test_answer_is_immutable_and_not_completion_acceptance(tmp_path):
    outbox = box(tmp_path)
    outbox.enqueue('q', 'question', 'Question', actor='leader')
    outbox.respond('q', 'yes', 'turn-1')
    outbox.respond('q', 'yes', 'turn-1')
    with pytest.raises(ValueError):
        outbox.respond('q', 'no', 'turn-2')
    outbox.enqueue('c', 'completion', 'Complete', actor='leader')
    with pytest.raises(ValueError):
        outbox.respond('c', 'accepted', 'turn-3')
