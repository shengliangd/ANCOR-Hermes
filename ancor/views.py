"""Compact, model-facing projections for progressive disclosure.

Full records remain available through explicit expansion paths. Previews are
never authoritative for exact values; callers must use the corresponding detail
tool before planning, accepting, or transcribing a decision.
"""

import json

DEFAULT_PREVIEW_CHARS = 280


def preview(value, limit=DEFAULT_PREVIEW_CHARS):
    """Return ``(preview, truncated)`` without changing short values."""
    if not isinstance(value, str):
        return value, False
    if limit < 0:
        raise ValueError('preview limit must not be negative')
    if len(value) <= limit:
        return value, False
    return value[:limit].rstrip() + '...', True


def task_summary(task, requirement_id=None):
    """Project a native task into the fields needed for planning and health."""
    summary = {
        'id': task['id'],
        'title': task.get('title'),
        'status': task.get('status'),
        'assignee': task.get('assignee'),
    }
    if requirement_id is not None:
        summary['requirement_id'] = requirement_id
    for key in ('priority', 'created_at', 'started_at', 'completed_at',
                'last_heartbeat_at', 'block_kind', 'block_recurrences'):
        value = task.get(key)
        if value is not None:
            summary[key] = value
    if task.get('archived_at') or task.get('archived'):
        summary['archived'] = True
    for field in ('blocked_reason', 'last_failure_error'):
        value, truncated = preview(task.get(field), 200)
        if value:
            summary[field + '_preview'] = value
            if truncated:
                summary[field + '_truncated'] = True
    return summary


def requirement_summary(requirement, draft=None):
    """Project a requirement; exact text requires an explicit detail read."""
    brief, brief_truncated = preview(requirement.get('brief'))
    criteria, criteria_truncated = preview(requirement.get('acceptance_criteria'))
    summary = {
        'id': requirement['id'],
        'status': requirement['status'],
        'replacement_of': requirement.get('replacement_of'),
    }
    if draft and draft.get('summary'):
        summary['summary'] = draft['summary']
    summary.update({
        'brief_preview': brief,
        'acceptance_criteria_preview': criteria,
        'truncated': brief_truncated or criteria_truncated,
    })
    if brief_truncated:
        summary['brief_truncated'] = True
    if criteria_truncated:
        summary['acceptance_criteria_truncated'] = True
    return summary


def draft_summary(draft):
    brief, brief_truncated = preview(draft.get('brief'))
    criteria, criteria_truncated = preview(draft.get('acceptance_criteria'))
    summary = {
        'id': draft['id'],
        'replacement_of': draft.get('replacement_of'),
        'summary': draft.get('summary'),
        'brief_preview': brief,
        'acceptance_criteria_preview': criteria,
        'truncated': brief_truncated or criteria_truncated,
    }
    if brief_truncated:
        summary['brief_truncated'] = True
    if criteria_truncated:
        summary['acceptance_criteria_truncated'] = True
    return summary


def notification_selection(rows, limit=20):
    """Keep unresolved questions visible before recent delivery notices."""
    if len(rows) <= limit:
        return list(rows)
    open_questions = [row for row in rows
                      if row.get('kind') == 'question' and row.get('answer') is None
                      and row.get('closed_at') is None]
    chosen = {row['id'] for row in open_questions[:limit]}
    if len(chosen) < limit:
        for row in reversed(rows):
            if len(chosen) >= limit:
                break
            chosen.add(row['id'])
    return [row for row in rows if row['id'] in chosen]


def notification_summary(row, message_limit=600, answer_limit=300):
    """Project a durable notification while preserving health-relevant fields."""
    message, message_truncated = preview(row.get('message'), message_limit)
    answer = row.get('answer')
    answer_preview, answer_truncated = preview(answer, answer_limit)
    summary = {
        'id': row['id'],
        'kind': row['kind'],
        'state': row['state'],
        'requirement_id': row.get('requirement_id'),
        'task_id': row.get('task_id'),
        'answered': answer is not None,
        'closed': bool(row.get('closed_at')),
        'answer': answer_preview,
        'message_preview': message,
    }
    if row.get('close_reason'):
        summary['close_reason'] = row['close_reason']
    if row.get('closed_by'):
        summary['closed_by'] = row['closed_by']
    if message_truncated:
        summary['message_truncated'] = True
    if answer_truncated:
        summary['answer_truncated'] = True
    error = row.get('error')
    if error:
        try:
            import json
            decoded = json.loads(error)
            summary['error_code'] = decoded.get('reason_code') or decoded.get('detail') or 'error'
        except (TypeError, ValueError):
            summary['error_code'] = str(error)[:120]
    return summary


def notification_detail(row):
    """Expose one complete notification without the transport-error key Hermes
    interprets as a failed tool result."""
    detail = dict(row)
    if 'error' in detail:
        detail['delivery_error'] = detail.pop('error')
    return detail


def transition_summary(event):
    """Project retained pause/resume history without transport identity."""
    payload = event.get('payload') or {}
    summary = {
        'id': event['id'],
        'kind': event['kind'],
        'requirement_id': payload.get('requirement_id'),
        'command_id': payload.get('command_id'),
    }
    for field in ('reason', 'summary'):
        value, truncated = preview(payload.get(field), 300)
        if value:
            summary[field] = value
            if truncated:
                summary[field + '_truncated'] = True
    return summary


_EVENT_REFERENCE_KEYS = (
    'requirement_id', 'task_id', 'task_ids', 'notification_id', 'draft_id',
    'cancellation_id', 'key',
)


def event_summary(event, payload_limit=600):
    """Project one durable event while retaining its acknowledgement identity."""
    payload = event.get('payload')
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload_preview, payload_truncated = preview(encoded, payload_limit)
    summary = {
        'id': event['id'],
        'kind': event['kind'],
        'payload_preview': payload_preview,
    }
    if payload_truncated:
        summary['payload_truncated'] = True
    if isinstance(payload, dict):
        for key in _EVENT_REFERENCE_KEYS:
            if payload.get(key) is not None:
                summary[key] = payload[key]
    return summary


def event_summaries(events, limit):
    """Keep host health events and the newest input visible under a tight cap."""
    if not events:
        return []
    priority = [event for kind in ('host.stalled', 'host.liveness')
                for event in events if event['kind'] == kind]
    rest = [event for event in events if event['kind'] not in
            {'host.stalled', 'host.liveness'}]
    ordered = priority + rest
    if len(ordered) > limit:
        ordered = ordered[:max(0, limit - 1)] + [events[-1]]
        seen, unique = set(), []
        for event in ordered:
            if event['id'] not in seen:
                seen.add(event['id'])
                unique.append(event)
        ordered = unique
    return [event_summary(event) for event in ordered]
