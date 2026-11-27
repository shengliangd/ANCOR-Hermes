"""Tests for Independent Delivery Judge Gate (accept | reject | escalate + max_rejects cap)."""
import pytest

from ancor.goals import GoalStore
from ancor.judge import JudgeGate
from ancor.store import Store


def _make_request(store: Store) -> dict:
    p = store.propose_change(
        None,
        [{'op': 'create', 'title': 'Judge Test', 'fields': {
            'outcome': ['Produce clean dataset and quality report'],
            'constraint': ['Do not modify raw input files'],
        }}],
        actor='ancor-interactor',
        source={'kind': 'interactor_proposal', 'input': 'clean dataset'},
    )
    rid = store.commit_change(p['id'], 'confirm-1')['request_id']
    return store.get_request(rid)


def _valid_evidence(store: Store, rid: str) -> dict:
    content = store.content(rid)
    coverage = []
    for field in ('outcome', 'constraint'):
        for point in content['fields'][field]:
            coverage.append({
                'version_id': point['version_id'],
                'satisfied': True,
                'evidence': f'Independently verified {field}: {point["body"]}',
            })
    return {
        'generation': content['generation'],
        'coverage': coverage,
        'verification': ['Re-ran eval script and verified checksums'],
        'artifacts': ['/tmp/clean/dataset.parquet', '/tmp/clean/report.md'],
    }


def test_judge_accept_moves_request_to_awaiting_acceptance(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store)
    judge = JudgeGate(store, goals, max_rejects=3)

    packet = judge.build_judge_packet(
        req['id'], summary='Cleaned 12 CSVs',
        artifacts=[{'path': '/tmp/clean/report.md', 'desc': 'quality report'}],
        eval_evidence='All 12 files passed schema check',
    )
    assert packet['request']['generation'] == 1
    assert len(packet['request']['fields']['outcome']) == 1

    verdict = judge.apply_verdict(
        req['id'], generation=1, decision='accept',
        summary='All outcomes and constraints verified',
        evidence=_valid_evidence(store, req['id']),
        artifacts=[{'path': '/tmp/clean/report.md', 'desc': 'quality report'}],
    )
    assert verdict['decision'] == 'accept'
    assert verdict['request_status'] == 'awaiting_acceptance'
    assert store.get_request(req['id'])['status'] == 'awaiting_acceptance'

    # Human can now finalize acceptance via alignment layer!
    accepted = store.accept(req['id'], 'human-confirmed-ref')
    assert accepted['status'] == 'accepted'


def test_judge_reject_and_max_reject_cap_auto_escalates(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store)
    judge = JudgeGate(store, goals, max_rejects=2)

    # First reject -> stays active, emits judge.rejected to Root Agent
    r1 = judge.apply_verdict(
        req['id'], generation=1, decision='reject',
        summary='Missing deduplication count in report',
        feedback=['Add rejected row count table to report.md'],
    )
    assert r1['decision'] == 'reject'
    assert r1['attempt'] == 1
    assert store.get_request(req['id'])['status'] == 'active'
    assert any(e['kind'] == 'judge.rejected' for e in store.pending_events())

    # Second consecutive reject hits max_rejects=2 -> auto-escalates to Interactor!
    r2 = judge.apply_verdict(
        req['id'], generation=1, decision='reject',
        summary='Still missing deduplication count',
        feedback=['Deduplication criteria ambiguous between exact vs key-based'],
    )
    assert r2['decision'] == 'escalate'
    assert r2['escalated_from_reject_cap'] is True
    questions = store.questions(req['id'])
    assert len(questions) == 1
    assert questions[0]['id'] == r2['question_id']
    assert questions[0]['state'] == 'open'


def test_judge_direct_escalate_creates_interactor_question(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    goals = GoalStore(store)
    judge = JudgeGate(store, goals, max_rejects=3)

    esc = judge.apply_verdict(
        req['id'], generation=1, decision='escalate',
        summary='Requirement is infeasible: raw files are corrupted beyond recovery',
        feedback=['Confirm whether to drop 3 unreadable files or abort'],
    )
    assert esc['decision'] == 'escalate'
    assert esc['escalated_from_reject_cap'] is False
    qs = store.questions(req['id'])
    assert len(qs) == 1
    assert 'corrupted' in qs[0]['body']


@pytest.mark.parametrize('decision', ['accept', 'reject', 'escalate'])
def test_delivery_receipt_is_atomic_and_idempotent(tmp_path, decision, monkeypatch):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    gate = JudgeGate(store, GoalStore(store))
    packet = gate.build_judge_packet(req['id'], summary='done', artifacts=[])
    store.add_event('delivery.submitted', {'request_id': req['id'], 'judge_packet': packet})
    eid = next(e['id'] for e in store.pending_events() if e['kind'] == 'delivery.submitted')
    kwargs = dict(generation=req['generation'], decision=decision, summary='Independent decision',
                  feedback=['Needs evidence'], evidence=_valid_evidence(store, req['id']), delivery_event_id=eid)
    original = store._add_event
    def fail_after_side_effect(db, kind, *args, **kw):
        if kind.startswith('judge.'):
            raise RuntimeError('simulated crash before verdict commit')
        return original(db, kind, *args, **kw)
    monkeypatch.setattr(store, '_add_event', fail_after_side_effect)
    with pytest.raises(RuntimeError, match='simulated crash'):
        gate.apply_verdict(req['id'], **kwargs)
    assert store.get_request(req['id'])['status'] == 'active'
    assert gate.list_verdicts(req['id']) == []
    assert any(e['id'] == eid for e in store.pending_events())
    monkeypatch.setattr(store, '_add_event', original)
    first = gate.apply_verdict(req['id'], **kwargs)
    second = gate.apply_verdict(req['id'], **kwargs)
    assert first == second
    assert len(gate.list_verdicts(req['id'])) == 1
    assert not any(e['id'] == eid for e in store.pending_events())
