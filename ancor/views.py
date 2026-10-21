"""Compact, model-facing projections for progressive disclosure.

Raw store and native records are internal. Model-facing code must return one of
the projections in this module: collection items, one exact detail, or the wake
packet assembled by ``host.py``. Exact text is never authoritative until read
through a single-object detail path.
"""

import json

TITLE_MAX_CHARS = 80
DEFAULT_PREVIEW_CHARS = 160
MESSAGE_PREVIEW_CHARS = 240
ANSWER_PREVIEW_CHARS = 160
EVENT_PAYLOAD_PREVIEW_CHARS = 300
REASON_PREVIEW_CHARS = 160
TASK_COMMENT_LIMIT = 10
TASK_EVENT_LIMIT = 10
TASK_RUN_LIMIT = 5
TRANSITION_LIMIT = 20
DRAFT_LIMIT = 20
WAKE_LIMIT = 50


def collection(items, total=None):
    """Return the single collection envelope used by every list surface."""
    items = list(items)
    total = len(items) if total is None else int(total)
    return {'items': items, 'count': total, 'returned': len(items),
            'truncated': len(items) < total}


def preview(value, limit=DEFAULT_PREVIEW_CHARS):
    """Return ``(preview, truncated)`` without changing short values."""
    if not isinstance(value, str):
        return value, False
    if limit < 0:
        raise ValueError('preview limit must not be negative')
    if len(value) <= limit:
        return value, False
    return value[:limit].rstrip() + '...', True


def _preview_field(target, field, value, limit=DEFAULT_PREVIEW_CHARS):
    if value is None:
        return
    shown, truncated = preview(value, limit)
    if shown:
        target[field + '_preview'] = shown
        if truncated:
            target[field + '_truncated'] = True


def requirement_ref(requirement, progression=None):
    """Identity and health for one requirement in a collection."""
    item = {
        'id': requirement['id'],
        'title': requirement['title'],
        'status': requirement['status'],
        'replacement_of': requirement.get('replacement_of'),
    }
    if progression is not None:
        item['progression'] = {
            'state': progression.get('state'),
            'reason': progression.get('reason'),
            'waiting_on': progression.get('waiting_on'),
        }
    return item


def requirement_detail(requirement, draft=None):
    """Exact, single-requirement detail. Only this projection exposes brief/ac."""
    item = {
        'id': requirement['id'],
        'title': requirement['title'],
        'status': requirement['status'],
        'replacement_of': requirement.get('replacement_of'),
        'brief': requirement['brief'],
        'acceptance_criteria': requirement['acceptance_criteria'],
    }
    if draft and draft.get('summary'):
        item['summary'] = draft['summary']
    return item


def draft_summary(draft):
    brief, brief_truncated = preview(draft.get('brief'), DEFAULT_PREVIEW_CHARS)
    criteria, criteria_truncated = preview(
        draft.get('acceptance_criteria'), DEFAULT_PREVIEW_CHARS)
    item = {
        'id': draft['id'],
        'title': draft['title'],
        'replacement_of': draft.get('replacement_of'),
        'brief_preview': brief,
        'acceptance_criteria_preview': criteria,
    }
    _preview_field(item, 'summary', draft.get('summary'), DEFAULT_PREVIEW_CHARS)
    item['truncated'] = bool(brief_truncated or criteria_truncated
                             or item.get('summary_truncated'))
    if brief_truncated:
        item['brief_truncated'] = True
    if criteria_truncated:
        item['acceptance_criteria_truncated'] = True
    return item


def task_ref(task, requirement_id=None, requirement_title=None):
    """Short native-task identity used in all lists and nested references."""
    item = {
        'id': task['id'],
        'title': task.get('title'),
        'status': task.get('status'),
        'assignee': task.get('assignee'),
    }
    if requirement_id is not None:
        item['requirement_id'] = requirement_id
    if requirement_title is not None:
        item['requirement_title'] = requirement_title
    for key in ('priority', 'created_at', 'started_at', 'completed_at',
                'last_heartbeat_at', 'block_kind', 'block_recurrences'):
        value = task.get(key)
        if value is not None:
            item[key] = value
    if task.get('archived_at') or task.get('archived'):
        item['archived'] = True
    _preview_field(item, 'blocked_reason', task.get('blocked_reason'),
                   REASON_PREVIEW_CHARS)
    _preview_field(item, 'last_failure_error', task.get('last_failure_error'),
                   REASON_PREVIEW_CHARS)
    return item


def bounded_latest(items, limit, *, id_key='id', before_id=None):
    """Return a bounded chronological slice plus the true source count."""
    rows = list(items)
    if before_id is not None:
        rows = [row for row in rows
                if isinstance(row.get(id_key), int) and row[id_key] < before_id]
    total = len(rows)
    if limit < 1:
        raise ValueError('limit must be positive')
    return collection(rows[-limit:], total=total)


def task_detail(detail, *, requirement_id=None, requirement_title=None,
                comments_limit=TASK_COMMENT_LIMIT,
                events_limit=TASK_EVENT_LIMIT, runs_limit=TASK_RUN_LIMIT):
    """One exact card plus bounded history collections."""
    task = detail['task']
    item = task_ref(task, requirement_id, requirement_title)
    item.update({
        'body': task.get('body'),
        'latest_summary': detail.get('latest_summary'),
        'parents': list(detail.get('parents') or ()),
        'children': list(detail.get('children') or ()),
        'comments': bounded_latest(detail.get('comments') or (), comments_limit),
        'events': bounded_latest(detail.get('events') or (), events_limit),
        'runs': bounded_latest(detail.get('runs') or (), runs_limit),
    })
    return item


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


def notification_summary(row):
    """Project one durable notification for a collection."""
    answer = row.get('answer')
    answer_preview, answer_truncated = preview(answer, ANSWER_PREVIEW_CHARS)
    summary = {
        'id': row['id'],
        'kind': row['kind'],
        'state': row['state'],
        'requirement_id': row.get('requirement_id'),
        'task_id': row.get('task_id'),
        'answered': answer is not None,
        'closed': bool(row.get('closed_at')),
        'answer_preview': answer_preview,
    }
    _preview_field(summary, 'message', row.get('message'), MESSAGE_PREVIEW_CHARS)
    if row.get('close_reason'):
        _preview_field(summary, 'close_reason', row['close_reason'], REASON_PREVIEW_CHARS)
    if row.get('closed_by'):
        summary['closed_by'] = row['closed_by']
    if answer_truncated:
        summary['answer_truncated'] = True
    error = row.get('error')
    if error:
        try:
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
        _preview_field(summary, field, payload.get(field), DEFAULT_PREVIEW_CHARS)
    return summary


_EVENT_REFERENCE_KEYS = (
    'requirement_id', 'task_id', 'task_ids', 'notification_id', 'draft_id',
    'cancellation_id', 'key',
)


def event_summary(event, payload_limit=EVENT_PAYLOAD_PREVIEW_CHARS):
    """Project one durable event while retaining its acknowledgement identity."""
    payload = event.get('payload')
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload_preview, payload_truncated = preview(encoded, payload_limit)
    summary = {
        'id': event['id'],
        'kind': event['kind'],
        'payload_preview': payload_preview,
    }
    if event.get('source_key'):
        summary['source_key'] = event['source_key']
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


def wake_summary(wake):
    """Project one durable wake without its potentially long reason."""
    item = {
        'id': wake['id'],
        'requirement_id': wake['requirement_id'],
        'due_at': wake['due_at'],
        'state': wake['state'],
    }
    _preview_field(item, 'reason', wake.get('reason'), REASON_PREVIEW_CHARS)
    return item


wake_ref = wake_summary
