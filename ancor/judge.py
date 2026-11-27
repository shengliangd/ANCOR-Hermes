"""Independent delivery gate: Judge Agent (accept | reject | escalate).

Triggered only when a request initiates delivery. Runs in a fresh, clean context
outside the execution tree — never sharing execution-tree conversation history.
Inputs are strictly the authoritative requirement text at current generation plus
declared artifacts / evaluation evidence.
"""
from __future__ import annotations

import json
import time
from uuid import uuid4

from .goals import GoalStore
from .request_contract import FIELDS

DEFAULT_MAX_JUDGE_REJECTS = 3
VALID_DECISIONS = frozenset({'accept', 'reject', 'escalate'})


class JudgeGate:
    """Deterministic state machine for independent delivery verdicts."""

    def __init__(self, store, goal_store: GoalStore, *,
                 max_rejects: int = DEFAULT_MAX_JUDGE_REJECTS,
                 clock=time.time,
                 notification_outbox=None):
        if type(max_rejects) is not int or max_rejects < 1:
            raise ValueError('max_rejects must be a positive integer')
        self.store = store
        self.goal_store = goal_store
        self.max_rejects = max_rejects
        self.clock = clock
        self.notification_outbox = notification_outbox

    def build_judge_packet(self, request_id: str, *, summary: str,
                           artifacts: list[dict], eval_evidence: str | None = None) -> dict:
        """Assemble the clean, context-isolated input packet for the Judge Agent."""
        req = self.store.get_request(request_id)
        if req['status'] != 'active' or req.get('target_status') is not None:
            raise ValueError(f'request {request_id} is not stably active')
        content = self.store.content(request_id)
        grants = self.store.active_grants(request_id)
        goals = self.goal_store.list_goals(request_id=request_id, include_terminal=True)
        goal_results = [
            g['result'] for g in goals
            if g.get('result') is not None
        ]
        return {
            'request': {
                'id': req['id'],
                'title': req['title'],
                'generation': req['generation'],
                'content_mode': req['content_mode'],
                'fields': {
                    field: [
                        {'point_id': p['point_id'], 'version_id': p['version_id'], 'body': p['body']}
                        for p in content['fields'][field]
                    ]
                    for field in FIELDS
                },
                **({'brief': req['brief']} if req['content_mode'] != 'points' else {}),
                'grants': [
                    {k: g[k] for k in ('id', 'action', 'target', 'scope')}
                    for g in grants
                ],
            },
            'delivery': {
                'summary': summary,
                'artifacts': artifacts,
                'eval_evidence': eval_evidence,
                'goal_results': goal_results,
            },
        }

    def list_verdicts(self, request_id: str, generation: int | None = None) -> list[dict]:
        with self.store._transaction() as db:
            if generation is None:
                rows = db.execute(
                    'SELECT * FROM judge_verdicts WHERE request_id=? ORDER BY id',
                    (request_id,)).fetchall()
            else:
                rows = db.execute(
                    'SELECT * FROM judge_verdicts WHERE request_id=? AND generation=? ORDER BY id',
                    (request_id, generation)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item['feedback'] = json.loads(item['feedback'])
                item['artifacts'] = json.loads(item['artifacts'])
                item['evidence'] = json.loads(item['evidence']) if item['evidence'] else None
                result.append(item)
            return result

    def _consecutive_rejects(self, db, request_id: str, generation: int) -> int:
        rows = db.execute(
            'SELECT decision FROM judge_verdicts WHERE request_id=? AND generation=? ORDER BY id DESC',
            (request_id, generation)).fetchall()
        count = 0
        for row in rows:
            if row['decision'] == 'reject':
                count += 1
            else:
                break
        return count

    def apply_verdict(self, request_id: str, *, generation: int, decision: str,
                      summary: str, evidence: dict | None = None,
                      feedback: list[str] | None = None,
                      artifacts: list[dict] | None = None,
                      actor: str = 'ancor-judge',
                      runtime: list[dict] | None = None) -> dict:
        """Apply one Judge Final Decision: accept, reject (bounded), or escalate."""
        if decision not in VALID_DECISIONS:
            raise ValueError(f'decision must be one of {sorted(VALID_DECISIONS)}')
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError('summary must be a non-empty string')
        artifacts = list(artifacts or [])
        feedback = list(feedback or [])
        runtime_json = json.dumps(list(runtime or []), ensure_ascii=False)
        now = float(self.clock())

        req = self.store.get_request(request_id)
        if req['status'] != 'active' or req.get('target_status') is not None:
            raise ValueError(f'request {request_id} is not stably active')
        if req['generation'] != generation:
            raise RuntimeError(
                f"verdict generation {generation} does not match current request generation {req['generation']}")

        with self.store._transaction() as db:
            prior_rejects = self._consecutive_rejects(db, request_id, generation)

        effective_decision = decision
        escalated_from_reject_cap = False
        if decision == 'reject':
            if not feedback:
                raise ValueError('reject decision requires non-empty structured feedback')
            if prior_rejects + 1 >= self.max_rejects:
                effective_decision = 'escalate'
                escalated_from_reject_cap = True

        attempt = prior_rejects + 1

        if effective_decision == 'accept':
            if not isinstance(evidence, dict):
                raise ValueError('accept decision requires structured verification evidence')
            if req['content_mode'] == 'points':
                evidence_payload = {**evidence, 'generation': generation}
            else:
                evidence_payload = {**evidence, 'brief': req['brief']}
            updated_req = self.store.submit_acceptance(
                request_id, json.dumps(evidence_payload, ensure_ascii=False), generation=generation)
            with self.store._transaction() as db:
                cursor = db.execute(
                    'INSERT INTO judge_verdicts('
                    'request_id, generation, decision, summary, feedback, artifacts, evidence, attempt, runtime, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (request_id, generation, 'accept', summary, '[]',
                     json.dumps(artifacts, ensure_ascii=False),
                     json.dumps(evidence_payload, ensure_ascii=False), attempt, runtime_json, now))
                verdict_id = cursor.lastrowid
                self.store._add_event(
                    db, 'judge.accepted',
                    {'request_id': request_id, 'generation': generation,
                     'verdict_id': verdict_id, 'summary': summary},
                    acknowledged=True)
            return {
                'verdict_id': verdict_id,
                'decision': 'accept',
                'request_status': updated_req['status'],
                'generation': generation,
                'summary': summary,
            }

        if effective_decision == 'reject':
            with self.store._transaction() as db:
                cursor = db.execute(
                    'INSERT INTO judge_verdicts('
                    'request_id, generation, decision, summary, feedback, artifacts, evidence, attempt, runtime, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)',
                    (request_id, generation, 'reject', summary,
                     json.dumps(feedback, ensure_ascii=False),
                     json.dumps(artifacts, ensure_ascii=False), attempt, runtime_json, now))
                verdict_id = cursor.lastrowid
                self.store._add_event(
                    db, 'judge.rejected',
                    {'request_id': request_id, 'generation': generation,
                     'verdict_id': verdict_id, 'attempt': attempt,
                     'max_rejects': self.max_rejects,
                     'summary': summary, 'feedback': feedback})
            return {
                'verdict_id': verdict_id,
                'decision': 'reject',
                'attempt': attempt,
                'max_rejects': self.max_rejects,
                'request_status': 'active',
                'generation': generation,
                'summary': summary,
                'feedback': feedback,
            }

        # effective_decision == 'escalate'
        escalation_reason = (
            f'Judge reached max consecutive reject limit ({self.max_rejects}): {summary}'
            if escalated_from_reject_cap else summary
        )
        question_id = f'judge-esc-{request_id[:8]}-{generation}-{uuid4().hex[:6]}'
        question_body = escalation_reason
        if feedback:
            question_body += '\n\nDetails:\n' + '\n'.join(f'- {item}' for item in feedback)
        question = self.store.create_question(
            request_id, identifier=question_id, body=question_body,
            reason=escalation_reason, actor=actor)
        with self.store._transaction() as db:
            cursor = db.execute(
                'INSERT INTO judge_verdicts('
                'request_id, generation, decision, summary, feedback, artifacts, evidence, attempt, runtime, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)',
                (request_id, generation, 'escalate', escalation_reason,
                 json.dumps(feedback, ensure_ascii=False),
                 json.dumps(artifacts, ensure_ascii=False), attempt, runtime_json, now))
            verdict_id = cursor.lastrowid
            self.store._add_event(
                db, 'judge.escalated',
                {'request_id': request_id, 'generation': generation,
                 'verdict_id': verdict_id, 'question_id': question['id'],
                 'summary': escalation_reason,
                 'from_reject_cap': escalated_from_reject_cap},
                acknowledged=True)
        return {
            'verdict_id': verdict_id,
            'decision': 'escalate',
            'escalated_from_reject_cap': escalated_from_reject_cap,
            'question_id': question['id'],
            'request_status': 'active',
            'generation': generation,
            'summary': escalation_reason,
            'feedback': feedback,
        }
