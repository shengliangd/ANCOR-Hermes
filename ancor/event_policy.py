"""Coordinator wake policy and low-signal native event coalescing."""
import json


WAKE_EVENT_KINDS = frozenset({
    'completed', 'blocked', 'review_requested', 'dependency_wait',
    'gave_up', 'timed_out', 'spawn_failed', 'crashed', 'stale',
    'rate_limited', 'block_loop_detected', 'protocol_violation',
    'completion_blocked_hallucination', 'completion_blocked_empty_result',
    'suspected_hallucinated_references', 'changes_requested',
    'descendant_invalidated',
})


def payload_dict(event):
    """Return one native event payload as a mapping without mutating the event."""
    payload = event.get('payload') or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    return payload if isinstance(payload, dict) else {}


def is_initial_hold(event):
    """Creation holds are bookkeeping, not a worker block needing coordination."""
    details = payload_dict(event)
    return (event.get('kind') == 'blocked'
            and details.get('reason') == 'initial_status'
            and details.get('status') == 'blocked')


def is_wake_event(event):
    """True only for durable outcome/anomaly events that can require action."""
    return event.get('kind') in WAKE_EVENT_KINDS and not is_initial_hold(event)


def coalesce_native_events(events, *, limit=100):
    """Keep actionable events and collapse an ignored backlog to one watermark.

    Heartbeats and other bookkeeping events never wake the coordinator. When a
    page has fewer than ``limit`` actionable events, include the newest event as
    a cursor watermark so the caller can advance past the ignored backlog. The
    watermark is returned as-is and is rejected by :func:`is_wake_event`.
    """
    if limit < 1:
        raise ValueError('limit must be positive')
    rows = sorted(events, key=lambda event: event['id'])
    actionable = [event for event in rows if is_wake_event(event)]
    if len(actionable) >= limit:
        return actionable[:limit]
    selected = actionable
    if rows and (not selected or rows[-1]['id'] > selected[-1]['id']):
        selected = [*selected, rows[-1]]
    return selected
