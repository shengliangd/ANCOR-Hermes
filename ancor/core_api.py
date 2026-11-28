"""Platform-neutral, confirmation-free machine API facade for ANCOR.

Used by:
  - ``ancor core`` CLI (machine-facing control plane; assumes caller already confirmed)
  - ``ancor mcp`` server (per-role native tool calls for root / goal / judge / interactor)
  - ``ancor serve`` (read-only today, extensible to interactive mutations later)
  - Python callers / tests / host adapters
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from .atomic_tools import question_ref
from .config import AncorConfig, load_config
from .goals import GoalStore
from .judge import JudgeGate
from .memory import MemoryStore
from .notification import COMPLETION_MAX_CHARS, NotificationOutbox
from .quota_guard import QuotaGuard
from .store import Store
from .transitions import advance_request
from .views import (
    TRANSITION_LIMIT,
    actionable_notifications,
    awaiting_acceptances,
    bounded_latest,
    collection,
    completion_id,
    event_counts,
    event_summaries,
    is_open_question,
    note_detail,
    note_summary,
    notification_counts,
    notification_detail,
    notification_selection,
    notification_summary,
    request_content,
    request_counts,
    request_detail,
    request_ref,
    transition_summary,
)
from .visualizer import render_html, render_text_tree
from .watcher import ClusterBackend, FileClusterBackend


class _ConfigCategoryAdapter:
    """Lightweight category validator backed by AncorConfig.categories."""

    def __init__(self, config: AncorConfig):
        self.config = config

    def alias_names(self) -> frozenset[str]:
        return frozenset(self.config.categories.keys())

    def validate(self, category: str | None) -> str | None:
        from .model_routing import CategoryError, normalize_category
        cat = normalize_category(category)
        if cat is None:
            return None
        names = self.alias_names()
        if not names:
            raise CategoryError(
                f'category {cat!r} requested but no categories are defined in ANCOR config')
        if cat not in names:
            raise CategoryError(
                f'unknown category {cat!r}; configured categories are {sorted(names)}')
        return cat


class AncorCore:
    """Confirmation-free, platform-neutral facade over ANCOR's SQLite substrate."""

    def __init__(
        self,
        config: AncorConfig | None = None,
        *,
        store: Store | None = None,
        cluster_backend: ClusterBackend | None = None,
        category_router: Any = None,
        clock=time.time,
    ):
        self.config = config or load_config()
        self.clock = clock
        self.store = store or Store(self.config.store_path)
        self.cluster_backend = cluster_backend or FileClusterBackend(
            self.config.cluster_state_path, clock=clock)
        self.category_router = (
            category_router if category_router is not None
            else (_ConfigCategoryAdapter(self.config) if self.config.categories else None)
        )
        self.goal_store = GoalStore(
            self.store,
            clock=clock,
            global_limits=self.config.global_limits or None,
            category_router=self.category_router,
        )
        self.judge_gate = JudgeGate(
            self.store,
            self.goal_store,
            max_rejects=self.config.max_judge_rejects,
            clock=clock,
        )
        self.memory = MemoryStore(
            self.config.effective_memory_dir(),
            stale_days=self.config.memory_stale_days,
        )

    def outbox(self, *, actor: str = 'root') -> NotificationOutbox:
        return NotificationOutbox(
            self.config.notifications_path,
            coordinator_profile=actor,
            target='ancor:outbox',
            clock=self.clock,
        )

    def _notifications(self) -> list[dict]:
        path = self.config.notifications_path
        if not path.exists():
            return []
        rows = NotificationOutbox.read_existing(path)
        for row in rows:
            if row['answer'] is not None:
                self._record_response_event(row)
        return rows

    def _record_response_event(self, row: dict) -> dict:
        try:
            ref = json.loads(row.get('answer_reference') or '{}')
        except (TypeError, ValueError):
            ref = {}
        meta = ref if isinstance(ref, dict) else {}
        return self.store.add_event(
            'notification.response',
            {
                'notification_id': row['id'],
                'body': row['answer'],
                'request_id': row.get('request_id'),
                'reference': row['answer_reference'],
                'source': meta.get('source') or 'ancor_core',
                'confirmed': True,
                'authorization': False,
            },
            source_key='notification.response:' + row['id'],
        )

    def _default_ref(self, kind: str, *, actor: str, confirmation_ref: str | None = None) -> str:
        if confirmation_ref and confirmation_ref.strip():
            return confirmation_ref.strip()
        return json.dumps({
            'source': 'ancor_core',
            'kind': kind,
            'actor': actor,
            'at': float(self.clock()),
        }, sort_keys=True)

    def _base_versions(self, request_id: str) -> dict[str, int]:
        req = self.store.get_request(request_id)
        return {
            'generation': req['generation'],
            'title_version': req['title_version'],
            'control_version': req['control_version'],
            'notes_version': req['notes_version'],
        }

    # --- Overview & Reads ---

    def overview(
        self,
        *,
        include_events: bool = True,
        include_closed_notifications: bool = False,
        notification_limit: int = 10,
    ) -> dict:
        from .host import goal_summary
        data = self.store.snapshot()
        notification_rows = self._notifications()
        pending = data['pending_events']
        open_requests = [
            r for r in data['requests']
            if r['status'] in {'active', 'paused', 'pending'} or r.get('target_status') is not None
        ]
        active_goals = self.goal_store.list_goals(include_terminal=False)
        reviews = awaiting_acceptances(data['requests'], notification_rows)
        notification_source = (
            notification_rows if include_closed_notifications
            else actionable_notifications(notification_rows)
        )
        selected = notification_selection(notification_source, notification_limit)
        return {
            'requests': collection([request_ref(r, exact_via=True) for r in open_requests]),
            'active_goals': collection([goal_summary(g) for g in active_goals]),
            'resources': {
                **self.goal_store.total_active_usage(),
                'backend': self.cluster_backend.summary(),
            },
            'awaiting_acceptance': collection(reviews),
            'counts': {
                'requests': request_counts(data['requests']),
                'active_goals': len(active_goals),
                'events': event_counts(pending),
                'notifications': notification_counts(notification_rows),
                'awaiting_acceptance': len(reviews),
            },
            'notifications': collection(
                [notification_summary(row, exact_via=True) for row in selected],
                total=len(notification_source),
            ),
            'pending_events': collection(
                event_summaries(pending, 100, exact_via=True) if include_events else [],
                total=len(pending),
            ),
        }

    def request_list(self, *, include_closed: bool = False) -> dict:
        data = self.store.snapshot()
        rows = data['requests']
        if not include_closed:
            rows = [
                r for r in rows
                if r['status'] in {'active', 'paused', 'pending', 'awaiting_acceptance'}
                or r.get('target_status') is not None
            ]
        return collection([request_ref(r) for r in rows])

    def request_get(
        self,
        request_id: str,
        *,
        include_content: bool = False,
        include_history: bool = False,
        include_events: bool = True,
        include_closed_notifications: bool = False,
        notification_limit: int = 10,
        before_id: int | None = None,
    ) -> dict:
        from .host import goal_summary
        request = self.store.get_request(request_id)
        goals = self.goal_store.list_goals(request_id=request_id, include_terminal=True)
        verdicts = self.judge_gate.list_verdicts(request_id)
        notification_rows = self._notifications()
        relevant = [row for row in notification_rows if row.get('request_id') == request_id]
        eligible = relevant if include_closed_notifications else actionable_notifications(relevant)
        selected = notification_selection(eligible, notification_limit)
        content = request_content(
            self.store.content(request_id), request['brief'], expanded=include_content)
        grants = self.store.active_grants(request_id)
        result: dict[str, Any] = {
            'request': request_detail(request, include_text=False, include_audit=include_history),
            'content': content,
            'grants': (
                collection([{k: g[k] for k in ('id', 'action', 'target', 'scope')} for g in grants])
                if include_content else {'count': len(grants)}
            ),
            'goals': collection([goal_summary(g) for g in goals]),
            'judge_verdicts': collection(verdicts[-5:], total=len(verdicts)),
            'questions': collection([
                question_ref(q) for q in self.store.questions(request_id) if q['state'] == 'open'
            ]),
            'notifications': collection(
                [notification_summary(row, exact_via=True) for row in selected],
                total=len(eligible),
            ),
            'counts': {'notifications': notification_counts(relevant), 'goals': len(goals)},
        }
        transitions = self.store.request_transitions(request_id)
        if transitions:
            result['transitions'] = (
                bounded_latest([transition_summary(e) for e in transitions], TRANSITION_LIMIT, before_id=before_id)
                if include_history else {'count': len(transitions)}
            )
        if include_events:
            pending = self.store.pending_events()
            req_events = [e for e in pending if (e.get('payload') or {}).get('request_id') == request_id]
            result['pending_events'] = collection(
                event_summaries(req_events, 50, exact_via=True), total=len(req_events))
        return result

    def request_read(self, request_id: str, *, goal_id: str | None = None) -> dict:
        if goal_id and goal_id != 'root':
            return self.goal_store.read_request_for_goal(goal_id, request_id)
        req = self.store.get_request(request_id)
        content = self.store.content(request_id)
        grants = self.store.active_grants(request_id)
        return {
            'request': request_detail(req, include_text=True, include_audit=False),
            'content': content,
            'grants': [{k: g[k] for k in ('id', 'action', 'target', 'scope')} for g in grants],
        }

    # --- Direct (confirmation-free) & Two-Phase Change Mutations ---

    def change_propose(
        self,
        *,
        operations: list[dict],
        input_text: str,
        request_id: str | None = None,
        base: dict | None = None,
        actor: str = 'ancor-core',
        source_kind: str = 'core_api',
    ) -> dict:
        if request_id and base is None:
            base = self._base_versions(request_id)
        return self.store.propose_change(
            request_id,
            operations,
            actor=actor,
            source={'kind': source_kind, 'human_identity': None, 'input': input_text},
            base=base,
        )

    def change_commit(
        self,
        change_id: str,
        *,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        ref = self._default_ref('change', actor=actor, confirmation_ref=confirmation_ref)
        res = self.store.commit_change(change_id, ref)
        req_id = res.get('request_id')
        if req_id:
            try:
                res = {**res, 'request': request_detail(self.store.get_request(req_id), include_text=False)}
            except KeyError:
                pass
        return res

    def change_get(self, change_id: str) -> dict:
        return self.store.get_change(change_id)

    def change_history(
        self,
        request_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        include_notes: bool = False,
    ) -> dict:
        return self.store.list_changes(request_id, before=before, limit=limit, include_notes=include_notes)

    def request_create(
        self,
        *,
        title: str,
        fields: dict[str, list[str]],
        input_text: str | None = None,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        proposal = self.change_propose(
            operations=[{'op': 'create', 'title': title, 'fields': fields}],
            input_text=input_text or title,
            actor=actor,
        )
        return self.change_commit(proposal['id'], actor=actor, confirmation_ref=confirmation_ref)

    def request_revise(
        self,
        request_id: str,
        *,
        operations: list[dict],
        input_text: str,
        base: dict | None = None,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        proposal = self.change_propose(
            request_id=request_id,
            operations=operations,
            input_text=input_text,
            base=base,
            actor=actor,
        )
        return self.change_commit(proposal['id'], actor=actor, confirmation_ref=confirmation_ref)

    def request_pause(
        self,
        request_id: str,
        *,
        reason: str,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        return self.request_revise(
            request_id,
            operations=[{'op': 'pause', 'reason': reason}],
            input_text=reason,
            actor=actor,
            confirmation_ref=confirmation_ref,
        )

    def request_resume(
        self,
        request_id: str,
        *,
        reason: str,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        return self.request_revise(
            request_id,
            operations=[{'op': 'resume', 'reason': reason}],
            input_text=reason,
            actor=actor,
            confirmation_ref=confirmation_ref,
        )

    def request_cancel(
        self,
        request_id: str,
        *,
        reason: str = 'cancelled',
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        return self.request_revise(
            request_id,
            operations=[{'op': 'cancel', 'reason': reason}],
            input_text=reason,
            actor=actor,
            confirmation_ref=confirmation_ref,
        )

    def request_accept(
        self,
        request_id: str,
        *,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        verification = self.store.latest_verification(request_id)
        if verification is None:
            raise ValueError('verification required before acceptance')
        res = self.request_revise(
            request_id,
            operations=[{'op': 'accept', 'verification_id': verification['id']}],
            input_text='accept',
            actor=actor,
            confirmation_ref=confirmation_ref,
        )
        try:
            self.outbox().close_completions(
                request_id, closed_by=actor, reason='accepted_by_human')
        except Exception:
            pass
        return res

    def request_changes(
        self,
        request_id: str,
        *,
        feedback: str,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        res = self.request_revise(
            request_id,
            operations=[{'op': 'request_changes', 'feedback': feedback}],
            input_text=feedback,
            actor=actor,
            confirmation_ref=confirmation_ref,
        )
        try:
            self.outbox().close_completions(
                request_id, closed_by=actor, reason='changes_requested_by_human')
        except Exception:
            pass
        return res

    def request_advance(self, request_id: str) -> dict:
        return advance_request(
            self.store,
            self.goal_store,
            request_id,
            job_canceller=lambda h: self.cluster_backend.cancel_handle(h['handle_id'], h.get('tags')),
        )

    # --- Signals ---

    def signal_send(
        self,
        text: str,
        *,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        proposal = self.store.propose_signal(text, actor)
        ref = self._default_ref('signal', actor=actor, confirmation_ref=confirmation_ref)
        return self.store.confirm_signal(proposal['id'], ref)

    # --- Questions ---

    def question_list(
        self,
        *,
        request_id: str | None = None,
        include_closed: bool = False,
        limit: int = 50,
    ) -> dict:
        rows = self.store.questions(request_id)
        if not include_closed:
            rows = [r for r in rows if r['state'] == 'open']
        return collection([question_ref(r) for r in rows[:limit]], total=len(rows))

    def question_get(self, question_id: str, *, request_id: str | None = None) -> dict:
        for row in self.store.questions(request_id):
            if row['id'] == question_id:
                return row
        raise KeyError(question_id)

    def question_create(
        self,
        *,
        request_id: str,
        question_id: str,
        body: str,
        reason: str,
        point_ids: list[str] | None = None,
        actor: str = 'root',
    ) -> dict:
        return self.store.create_question(
            request_id,
            identifier=question_id,
            body=body,
            reason=reason,
            point_ids=point_ids or [],
            actor=actor,
        )

    def question_answer(
        self,
        question_id: str,
        *,
        body: str,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        q = self.question_get(question_id)
        return self.request_revise(
            q['request_id'],
            operations=[{
                'op': 'reply',
                'question_id': question_id,
                'revision': q['revision'],
                'body': body,
            }],
            input_text=body,
            actor=actor,
            confirmation_ref=confirmation_ref,
        )

    def question_withdraw(self, question_id: str, *, reason: str, actor: str = 'root') -> dict:
        return self.store.withdraw_question(question_id, reason, actor)

    # --- Notes ---

    def note_list(self, request_id: str, *, limit: int = 50, include_deleted: bool = False) -> dict:
        res = self.store.list_notes(request_id, limit=limit, include_deleted=include_deleted)
        return collection([note_summary(n) for n in res['items']], total=res['count'])

    def note_get(self, request_id: str, note_id: str) -> dict:
        return note_detail(self.store.get_note(request_id, note_id))

    def note_history(self, request_id: str, note_id: str) -> dict:
        return self.store.note_history(request_id, note_id)

    def note_add(self, request_id: str, body: str, *, actor: str = 'root') -> dict:
        return note_detail(self.store.add_note(request_id, body, actor=actor))

    def note_update(
        self, request_id: str, note_id: str, body: str, *, expected_revision: int, actor: str = 'root'
    ) -> dict:
        return note_detail(
            self.store.update_note(request_id, note_id, body, expected_revision=expected_revision, actor=actor))

    def note_delete(
        self, request_id: str, note_id: str, *, expected_revision: int, actor: str = 'root'
    ) -> dict:
        return note_detail(
            self.store.delete_note(request_id, note_id, expected_revision=expected_revision, actor=actor))

    # --- Events ---

    def event_get(self, event_id: int) -> dict:
        retained = self.store.events_by_ids([event_id])
        if not retained:
            raise KeyError(event_id)
        return {'event': retained[0]}

    def event_ack(self, event_ids: list[int], *, reason: str | None = None) -> dict:
        if not isinstance(event_ids, list) or not event_ids or any(type(i) is not int or i < 1 for i in event_ids):
            raise ValueError('event_ids must contain positive integer IDs')
        retained = self.store.events_by_ids(event_ids)
        by_id = {e['id']: e for e in retained}
        missing = [i for i in event_ids if i not in by_id]
        if missing:
            return {
                'acked': [],
                'already_acked': [],
                'rejected': [{'id': i, 'reason': 'not_found'} for i in missing],
                'remaining_pending': len(self.store.pending_events()),
            }
        acked, already = [], []
        for i in dict.fromkeys(event_ids):
            (already if by_id[i]['acknowledged'] else acked).append(i)
        if acked:
            self.store.ack_events(acked)
        result: dict[str, Any] = {
            'acked': acked,
            'already_acked': already,
            'rejected': [],
            'remaining_pending': len(self.store.pending_events()),
        }
        if reason is not None:
            result['reason'] = reason
        if acked:
            from .host import find_stalled_active_requests
            stalled = find_stalled_active_requests(self.store, self.goal_store)
            if stalled:
                result['stalled_requests'] = [
                    {
                        **item,
                        'warning': 'Active request now has 0 active goals, 0 open questions, and 0 pending events; spawn next /goal, create a question, or submit delivery.',
                    }
                    for item in stalled
                ]
        return result

    # --- Goals & Resources ---

    def _job_canceller(self, handle: dict) -> bool:
        return self.cluster_backend.cancel_handle(handle['handle_id'], handle.get('tags'))

    def goal_list(
        self,
        *,
        request_id: str | None = None,
        parent_goal_id: str | None = None,
        status: str | None = None,
        include_terminal: bool = False,
    ) -> dict:
        goals = self.goal_store.list_goals(
            request_id=request_id,
            parent_goal_id=parent_goal_id,
            status=status,
            include_terminal=include_terminal,
        )
        return collection(goals)

    def goal_get(self, goal_id: str) -> dict:
        goal = self.goal_store.get_goal(goal_id)
        children = self.goal_store.list_goals(parent_goal_id=goal_id, include_terminal=True)
        return {
            'goal': goal,
            'children': collection(children),
            'subtree_active_usage': self.goal_store.subtree_active_usage(goal_id),
        }

    def goal_spawn(self, spec: dict | str, *, caller_goal_id: str = 'root') -> dict:
        return self.goal_store.spawn_goal(spec, caller_goal_id=caller_goal_id)

    def goal_signal(
        self,
        goal_id: str,
        kind: str,
        *,
        message: str | None = None,
        generation: int | None = None,
        caller_goal_id: str = 'root',
    ) -> dict:
        payload: dict[str, Any] = {}
        if message is not None:
            payload['message'] = message
        if generation is not None:
            payload['generation'] = generation
        return self.goal_store.signal_goal(goal_id, kind, payload, caller_goal_id=caller_goal_id)

    def goal_pause(self, goal_id: str, *, mode: str = 'drain', caller_goal_id: str = 'root') -> dict:
        return self.goal_store.pause_goal(
            goal_id, mode=mode, caller_goal_id=caller_goal_id, job_canceller=self._job_canceller)

    def goal_resume(self, goal_id: str, *, caller_goal_id: str = 'root') -> dict:
        return self.goal_store.resume_goal(goal_id, caller_goal_id=caller_goal_id)

    def goal_stop(self, goal_id: str, *, reason: str = 'stopped', caller_goal_id: str = 'root') -> dict:
        return self.goal_store.stop_goal(
            goal_id, reason=reason, caller_goal_id=caller_goal_id, job_canceller=self._job_canceller)

    def goal_wait(
        self,
        goal_id: str,
        *,
        wake_after_seconds: float | None = None,
        wake_at: float | None = None,
        watch_path: str | None = None,
        reason: str = '',
    ) -> dict:
        return self.goal_store.wait_goal(
            goal_id,
            wake_after_seconds=wake_after_seconds,
            wake_at=wake_at,
            watch_path=watch_path,
            reason=reason,
        )

    def goal_update_budget(
        self,
        goal_id: str,
        budget: dict,
        *,
        reason: str | None = None,
        caller_goal_id: str = 'root',
    ) -> dict:
        return self.goal_store.update_goal_budget(
            goal_id, budget, caller_goal_id=caller_goal_id, reason=reason)

    def goal_report(self, goal_id: str, payload: dict) -> dict:
        try:
            return self.goal_store.report_result(goal_id, {'goal_id': goal_id, **payload})
        except RuntimeError as exc:
            if 'is already terminal' in str(exc):
                existing = self.goal_store.get_goal(goal_id)
                return {
                    'goal_id': goal_id,
                    'already_terminal': True,
                    'status': existing['status'],
                    'result': existing.get('result'),
                }
            raise

    def resource_acquire(
        self,
        goal_id: str,
        *,
        usage: dict | None = None,
        tags: dict | None = None,
        handle_id: str | None = None,
        command: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        guard = QuotaGuard(self.goal_store, self.cluster_backend)
        return guard.acquire(
            usage=usage,
            tags=tags,
            handle_id=handle_id,
            command=command,
            goal_id=goal_id,
            metadata=metadata,
        )

    def resource_release(
        self,
        goal_id: str,
        *,
        handle_id: str | None = None,
        status: str = 'released',
        detail: str | None = None,
    ) -> dict:
        res = self.goal_store.release_resource_usage(
            goal_id, handle_id=handle_id, status=status, detail=detail)
        if hasattr(self.cluster_backend, 'set_status'):
            backend_status = 'cancelled' if status == 'released' else status
            for hid in res.get('released', []):
                try:
                    self.cluster_backend.set_status(hid, backend_status, detail=detail)
                except KeyError:
                    pass
        return res

    def cluster_status(self) -> dict:
        return {
            **self.goal_store.total_active_usage(),
            'backend': self.cluster_backend.summary(),
        }

    # --- Delivery & Judge ---

    def delivery_submit(
        self,
        request_id: str,
        *,
        summary: str,
        artifacts: list[dict],
        eval_evidence: str | None = None,
    ) -> dict:
        packet = self.judge_gate.build_judge_packet(
            request_id,
            summary=summary,
            artifacts=artifacts,
            eval_evidence=eval_evidence,
        )
        event = self.store.add_event('delivery.submitted', {
            'request_id': request_id,
            'generation': packet['request']['generation'],
            'summary': summary,
            'artifacts': artifacts,
            'judge_packet': packet,
        })
        return {'status': 'submitted_to_judge', 'event_id': event['id'], 'judge_packet': packet}

    def judge_verdict(
        self,
        *,
        request_id: str,
        delivery_event_id: int | None,
        generation: int,
        decision: str,
        summary: str,
        evidence: dict | None = None,
        feedback: list[str] | None = None,
        artifacts: list[dict] | None = None,
        actor: str = 'ancor-judge',
        runtime: list[dict] | None = None,
    ) -> dict:
        return self.judge_gate.apply_verdict(
            request_id,
            delivery_event_id=delivery_event_id,
            generation=generation,
            decision=decision,
            summary=summary,
            evidence=evidence,
            feedback=feedback,
            artifacts=artifacts,
            actor=actor,
            runtime=runtime,
        )

    # --- Notifications ---

    def notification_list(
        self,
        *,
        notification_id: str | None = None,
        request_id: str | None = None,
        state: str | None = None,
        open_only: bool = False,
        limit: int = 50,
    ) -> dict:
        rows = self._notifications()
        if notification_id:
            rows = [r for r in rows if r['id'] == notification_id]
            if not rows:
                raise KeyError(notification_id)
        if state:
            rows = [r for r in rows if r['state'] == state]
        if request_id:
            rows = [r for r in rows if r.get('request_id') == request_id]
        if open_only:
            rows = [r for r in rows if is_open_question(r)]
        total = len(rows)
        rows = rows[-limit:]
        items = (
            [notification_detail(r) for r in rows] if notification_id
            else [notification_summary(r) for r in rows]
        )
        return {'notifications': collection(items, total=total)}

    def notification_send_completion(
        self,
        *,
        notification_id: str,
        request_id: str,
        message: str,
        actor: str = 'root',
    ) -> dict:
        req = self.store.get_request(request_id)
        if req['status'] != 'awaiting_acceptance':
            raise ValueError('completion requires a request awaiting human acceptance')
        if notification_id != completion_id(req):
            raise ValueError('completion id must be ' + completion_id(req))
        if len(message) > COMPLETION_MAX_CHARS:
            raise ValueError(f'completion message exceeds {COMPLETION_MAX_CHARS} characters')
        box = self.outbox(actor=actor)
        box.enqueue(notification_id, 'completion', message, actor=actor, request_id=request_id)
        return notification_detail(box.get(notification_id))

    def notification_respond(
        self,
        notification_id: str,
        *,
        answer: str,
        actor: str = 'ancor-core',
        confirmation_ref: str | None = None,
    ) -> dict:
        box = self.outbox()
        ref = self._default_ref('response', actor=actor, confirmation_ref=confirmation_ref)
        row = box.respond(notification_id, answer, ref)
        event = self._record_response_event(row)
        return {'notification': notification_detail(row), 'event': event, 'confirmed': True}

    # --- Visualize ---

    def visualize(self, *, request_id: str | None = None, output_path: str | None = None) -> dict:
        tree_text = render_text_tree(self.store.path, request_id=request_id)
        out: dict[str, Any] = {'tree': tree_text}
        if output_path:
            target = Path(output_path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_html(self.store.path, request_id=request_id), encoding='utf-8')
            out['html_path'] = str(target)
        return out

    # --- Memory ---

    def memory_index(
        self,
        *,
        role: str = 'human',
        scope: str | None = None,
        category: str | None = None,
    ) -> dict:
        items = self.memory.list_index(role=role, scope=scope, category=category)
        return {
            'memory_dir': str(self.memory.root),
            'items': items,
            'count': len(items),
            'index_markdown': self.memory.render_index_markdown(role=role),
        }

    def memory_read(self, path: str, *, role: str = 'human') -> dict:
        return self.memory.read(path, role=role)

    def memory_search(self, query: str, *, role: str = 'human', limit: int = 10) -> dict:
        hits = self.memory.search(query, role=role, limit=limit)
        return {'query': query, 'items': hits, 'count': len(hits)}

    def memory_write(
        self,
        path: str,
        content: str,
        *,
        role: str = 'human',
        expected_hash: str | None = None,
        scope: str | None = None,
        summary: str | None = None,
        verified_at: str | None = None,
        owner: str | None = None,
    ) -> dict:
        return self.memory.write(
            path,
            content,
            role=role,
            expected_hash=expected_hash,
            scope=scope,
            summary=summary,
            verified_at=verified_at,
            owner=owner,
        )

    def memory_prompt_context(self, *, role: str = 'goal', hostname: str | None = None) -> str:
        return self.memory.build_prompt_context(role=role, hostname=hostname)
