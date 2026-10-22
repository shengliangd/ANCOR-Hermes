"""Compact, model-facing projections for progressive disclosure.

Raw store and native records are internal. Model-facing code must return one of
the projections in this module: collection items, one exact detail, or the wake
packet assembled by ``host.py``. Exact text is never authoritative until read
through a single-object detail path.
"""

import json

TITLE_MAX_CHARS = 80
DEFAULT_PREVIEW_CHARS = 160
MESSAGE_PREVIEW_CHARS = 120
ANSWER_PREVIEW_CHARS = 160
ATTENTION_PREVIEW_CHARS = 80
EVENT_PAYLOAD_PREVIEW_CHARS = 300
REASON_PREVIEW_CHARS = 160
NOTE_PREVIEW_CHARS = 240
TASK_COMMENT_LIMIT = 10
TASK_EVENT_LIMIT = 10
TASK_RUN_LIMIT = 5
TRANSITION_LIMIT = 20
DRAFT_LIMIT = 20
WAKE_LIMIT = 50
TERMINAL_TASK_STATUSES = {'done', 'archived', 'cancelled'}
OPEN_NOTIFICATION_STATES = {'pending', 'sending', 'uncertain'}


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


def requirement_exact_via(requirement_id):
    return {'tool': 'ancor_get_context', 'args': {'requirement_id': requirement_id}}


def task_exact_via(task_id):
    return {'tool': 'ancor_board', 'args': {'action': 'show', 'task_id': task_id}}


def notification_exact_via(identifier):
    return {'tool': 'ancor_notify', 'args': {'action': 'list', 'id': identifier}}


def event_exact_via(event_id):
    return {'tool': 'ancor_get_context', 'args': {'event_id': event_id}}


def requirement_ref(requirement, progression=None, *, exact_via=False):
    """Identity and health for one requirement in a collection."""
    item = {
        'id': requirement['id'],
        'title': requirement['title'],
        'status': requirement['status'],
        'replacement_of': requirement.get('replacement_of'),
    }
    if requirement.get('target_status') is not None:
        item['target_status'] = requirement['target_status']
    if progression is not None:
        item['progression'] = {
            'state': progression.get('state'),
            'reason': progression.get('reason'),
            'waiting_on': progression.get('waiting_on'),
        }
    if exact_via:
        item['exact_via'] = requirement_exact_via(requirement['id'])
    return item


def requirement_progress(requirement, progression=None, tasks=(), notifications=()):
    """One compact row for the open-work progress table."""
    progression = progression or {}
    counts = task_counts(tasks)
    item = {
        'id': requirement['id'],
        'title': requirement['title'],
        'status': requirement['status'],
        'state': progression.get('state'),
        'reason': progression.get('reason'),
        'tasks': {'open': counts['open'], 'total': counts['total']},
    }
    if requirement.get('target_status') is not None:
        item['target_status'] = requirement['target_status']
    waiting_on = progression.get('waiting_on')
    if waiting_on:
        item['waiting_on'] = waiting_on
    open_questions = sum(
        1 for row in notifications
        if row.get('requirement_id') == requirement['id'] and is_open_question(row))
    if open_questions:
        item['open_questions'] = open_questions
    task_by_id = {task['id']: task for task in tasks}
    focus_ids = progression.get('task_ids') or ()
    focus = next((task_by_id[task_id] for task_id in focus_ids
                  if task_id in task_by_id), None)
    if focus is None:
        focus = next((task for task in tasks if not is_terminal_task(task)), None)
    if focus is not None:
        item['focus'] = {
            'id': focus['id'],
            'title': focus.get('title'),
            'status': focus.get('status'),
        }
        blocked_reason = focus.get('blocked_reason_preview')
        truncated = bool(focus.get('blocked_reason_truncated'))
        if blocked_reason is None:
            blocked_reason, truncated = preview(
                focus.get('blocked_reason'), REASON_PREVIEW_CHARS)
        if blocked_reason:
            item['focus']['blocked_reason_preview'] = blocked_reason
            if truncated:
                item['focus']['blocked_reason_truncated'] = True
    return item


def awaiting_acceptances(requirements, notifications):
    """Requirements whose coordinator-verified result needs the human's decision."""
    waiting = {requirement['id']: requirement for requirement in requirements or ()
               if requirement.get('status') == 'awaiting_acceptance'}
    latest = {}
    for row in notifications or ():
        if row.get('kind') != 'completion' or row.get('closed_at') is not None:
            continue
        requirement = waiting.get(row.get('requirement_id'))
        if requirement is not None:
            latest[requirement['id']] = row
    items = []
    for requirement_id, requirement in waiting.items():
        row = latest.get(requirement_id)
        result_preview, truncated = preview(
            row.get('message') if row else None, DEFAULT_PREVIEW_CHARS)
        item = {
            'id': requirement_id,
            'title': requirement['title'],
            'status': 'awaiting_acceptance',
            'state': 'awaiting_human_acceptance',
            'reason': 'human_acceptance_pending',
            'notice_id': 'accept:' + requirement_id,
            'result_preview': result_preview,
            'accept_via': {
                'tool': 'ancor_requirement',
                'args': {'action': 'accept', 'requirement_id': requirement_id},
            },
            'request_changes_via': {
                'tool': 'ancor_requirement',
                'args': {'action': 'request_changes', 'requirement_id': requirement_id,
                         'feedback': '<exact human change request>'},
            },
        }
        if row is not None:
            item['notice_state'] = row.get('state')
            item['notice_delivered'] = row.get('state') == 'sent'
        completed_at = (row.get('delivery_started_at') or row.get('queued_at')
                        if row is not None else None)
        if completed_at is not None:
            item['completed_at'] = completed_at
        if row is not None and truncated:
            item['result_truncated'] = True
        items.append(item)
    items.sort(key=lambda item: (item.get('completed_at') is not None,
                                 item.get('completed_at') or 0, item['id']),
               reverse=True)
    return items


def requirement_detail(requirement, draft=None):
    """Exact, single-requirement detail. Only this projection exposes brief/ac."""
    item = {
        'id': requirement['id'],
        'title': requirement['title'],
        'status': requirement['status'],
        'target_status': requirement.get('target_status'),
        'replacement_of': requirement.get('replacement_of'),
        'brief': requirement['brief'],
        'acceptance_criteria': requirement['acceptance_criteria'],
    }
    if draft and draft.get('summary'):
        item['summary'] = draft['summary']
    return item


def note_summary(note):
    """Compact requirement-note reference; full text requires note_detail."""
    body_preview, truncated = preview(note['body'], NOTE_PREVIEW_CHARS)
    item = {
        'id': note['id'],
        'requirement_id': note['requirement_id'],
        'body_preview': body_preview,
        'revision': note['revision'],
        'created_at': note['created_at'],
        'updated_at': note['updated_at'],
    }
    if truncated:
        item['body_truncated'] = True
    if note.get('deleted_at') is not None:
        item['deleted'] = True
    return item


def note_detail(note):
    """Full requirement note, including soft-deleted history."""
    item = {
        'id': note['id'],
        'requirement_id': note['requirement_id'],
        'body': note['body'],
        'revision': note['revision'],
        'created_at': note['created_at'],
        'updated_at': note['updated_at'],
    }
    if note.get('deleted_at') is not None:
        item['deleted'] = True
        item['deleted_at'] = note['deleted_at']
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


def task_ref(task, requirement_id=None, requirement_title=None, *, exact_via=False):
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
    if exact_via:
        item['exact_via'] = task_exact_via(task['id'])
    return item


def is_terminal_task(task):
    return (task.get('status') in TERMINAL_TASK_STATUSES
            or bool(task.get('archived_at') or task.get('archived')))


def select_tasks(tasks, *, include_done=False, include_archived=False):
    """Return linked task refs for the requested terminal-state policy."""
    selected = []
    for task in tasks or ():
        status = task.get('status')
        archived = status == 'archived' or bool(task.get('archived_at') or task.get('archived'))
        if archived and not include_archived:
            continue
        if status == 'done' and not include_done:
            continue
        if status == 'cancelled' and not include_done:
            continue
        selected.append(task)
    return selected


def task_counts(tasks):
    """Count tasks by native status without hiding terminal work."""
    counts = {}
    terminal = 0
    for task in tasks or ():
        status = task.get('status') or 'unknown'
        counts[status] = counts.get(status, 0) + 1
        if is_terminal_task(task):
            terminal += 1
    total = sum(counts.values())
    return {'total': total, 'open': total - terminal, 'terminal': terminal,
            'by_status': counts}


def is_open_question(row):
    return (row.get('kind') == 'question' and row.get('answer') is None
            and row.get('closed_at') is None)


def actionable_notifications(rows):
    """Keep unresolved human questions and delivery failures, not history."""
    return [row for row in rows or () if is_open_question(row)]


def notification_counts(rows):
    rows = list(rows or ())
    open_questions = [row for row in rows if is_open_question(row)]
    delivery_issues = [row for row in rows
                       if row.get('state') in OPEN_NOTIFICATION_STATES
                       and row.get('closed_at') is None]
    answered = [row for row in rows
                if row.get('answer') is not None and row.get('closed_at') is None]
    closed = [row for row in rows if row.get('closed_at') is not None]
    return {'total': len(rows), 'open_questions': len(open_questions),
            'delivery_issues': len(delivery_issues), 'answered': len(answered),
            'closed': len(closed)}


def requirement_counts(requirements):
    counts = {}
    for requirement in requirements or ():
        status = requirement.get('status') or 'unknown'
        counts[status] = counts.get(status, 0) + 1
    return counts


def event_counts(events):
    counts = {}
    for event in events or ():
        kind = event.get('kind') or 'unknown'
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def conditions_from_progression(progression):
    """Expose host-owned stalled conditions without making them ackable events."""
    conditions = []
    for item in progression or ():
        if item.get('state') != 'stalled':
            continue
        requirement_id = item.get('requirement_id')
        if not requirement_id:
            continue
        conditions.append({
            'id': 'stalled:' + requirement_id,
            'kind': 'stalled',
            'requirement_id': requirement_id,
            'state': 'open',
            'reason': item.get('reason'),
            'task_ids': list(item.get('task_ids') or ()),
            'ack_policy': 'host_owned',
            'since': None,
            'generation': 0,
            'last_woken_at': None,
            'exact_via': requirement_exact_via(requirement_id),
        })
    return conditions


_ATTENTION_PRIORITY = {
    'triage': 10,
    'stale_heartbeat': 20,
    'question_answered_not_resumed': 25,
    'question_delivery_unconfirmed': 25,
    'blocked_without_wait': 30,
    'question_superseded': 30,
    'acceptance_pending': 40,
    'no_work': 50,
    'no_active_task': 50,
}


def attention_items(progression, tasks=(), notifications=(), events=(), limit=8):
    """Small, actionable discovery list with a concrete expansion path."""
    items = []
    for item in progression or ():
        if item.get('state') != 'stalled':
            continue
        requirement_id = item.get('requirement_id')
        reason = item.get('reason')
        items.append({
            'kind': 'requirement',
            'id': requirement_id,
            'requirement_id': requirement_id,
            'task_ids': list(item.get('task_ids') or ()),
            'why_code': reason,
            'preview': reason,
        })
    for event in events or ():
        if event.get('kind') == 'host.stalled':
            continue
        summary = event_summary(event, exact_via=True)
        items.append({
            'kind': 'event',
            'id': event['id'],
            'event_kind': event.get('kind'),
            'requirement_id': summary.get('requirement_id'),
            'task_id': summary.get('task_id'),
            'why_code': event.get('kind'),
            'preview': preview(summary.get('payload_preview') or event.get('kind'),
                               ATTENTION_PREVIEW_CHARS)[0],
        })
    for row in notifications or ():
        if not is_open_question(row):
            continue
        items.append({
            'kind': 'notification',
            'id': row['id'],
            'requirement_id': row.get('requirement_id'),
            'task_id': row.get('task_id'),
            'why_code': 'open_question',
        })
    seen = set()
    unique = []
    for item in sorted(items, key=lambda row: (
            _ATTENTION_PRIORITY.get(row.get('why_code'), 35),
            str(row.get('id')))):
        key = (item['kind'], str(item.get('id')))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:limit]


def omitted_groups(tasks, notifications):
    """Describe excluded history with counts and executable follow-up queries."""
    task_rows = list(tasks or ())
    notification_rows = list(notifications or ())
    omitted = []
    buckets = {'done': 0, 'archived': 0, 'cancelled': 0}
    for task in task_rows:
        if task.get('archived_at') or task.get('archived'):
            buckets['archived'] += 1
        elif task.get('status') in {'done', 'cancelled'}:
            buckets[task['status']] += 1
    for status in ('done', 'archived', 'cancelled'):
        count = buckets[status]
        if count:
            omitted.append({
                'kind': 'tasks', 'reason': status, 'count': count,
                'query': {'tool': 'ancor_board', 'args': (
                    {'action': 'list', 'include_archived': True}
                    if status == 'archived' else
                    {'action': 'list', 'status': status})},
            })
    closed = sum(1 for row in notification_rows if row.get('closed_at') is not None)
    answered = sum(1 for row in notification_rows
                   if row.get('answer') is not None and row.get('closed_at') is None)
    if closed:
        omitted.append({
            'kind': 'notifications', 'reason': 'closed', 'count': closed,
            'query': {'tool': 'ancor_notify', 'args': {'action': 'list'}},
        })
    if answered:
        omitted.append({
            'kind': 'notifications', 'reason': 'answered', 'count': answered,
            'query': {'tool': 'ancor_notify', 'args': {'action': 'list'}},
        })
    return omitted


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


def acceptance_ref(requirement, tasks=(), acceptance=None):
    """State whether exact acceptance is recorded, pending, or not yet possible."""
    tasks = list(tasks or ())
    done = sum(1 for task in tasks if task.get('status') in {'done', 'archived'})
    event_kind = acceptance.get('kind') if acceptance is not None else None
    if requirement.get('status') == 'accepted' or event_kind == 'requirement.accepted':
        status = 'accepted'
    elif requirement.get('status') == 'awaiting_acceptance':
        status = 'awaiting_human'
    elif event_kind == 'requirement.changes_requested':
        status = 'changes_requested'
    elif tasks and done == len(tasks):
        status = 'pending'
    else:
        status = 'not_ready'
    return {'status': status, 'tasks_done': done, 'tasks_total': len(tasks)}


def notification_summary(row, *, exact_via=False):
    """Project one durable notification for a collection."""
    answer = row.get('answer')
    answer_preview, answer_truncated = preview(answer, ANSWER_PREVIEW_CHARS)
    summary = {
        'id': row['id'],
        'kind': row['kind'],
        'state': row['state'],
        'answered': answer is not None,
        'closed': bool(row.get('closed_at')),
    }
    for field in ('requirement_id', 'task_id'):
        if row.get(field) is not None:
            summary[field] = row[field]
    if answer_preview:
        summary['answer_preview'] = answer_preview
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
    if exact_via:
        summary['exact_via'] = notification_exact_via(row['id'])
    return summary


def notification_detail(row):
    """Expose one complete notification without the transport-error key Hermes
    interprets as a failed tool result."""
    detail = dict(row)
    if 'error' in detail:
        detail['delivery_error'] = detail.pop('error')
    return detail


def transition_summary(event):
    """Project retained requirement-transition history without transport identity."""
    payload = event.get('payload') or {}
    summary = {
        'id': event['id'],
        'kind': event['kind'],
        'requirement_id': payload.get('requirement_id'),
        'command_id': payload.get('command_id'),
        'from_status': payload.get('from_status'),
        'target_status': payload.get('target_status'),
        'replacement_id': payload.get('replacement_id'),
    }
    for field in ('reason', 'summary'):
        _preview_field(summary, field, payload.get(field), DEFAULT_PREVIEW_CHARS)
    return summary


_EVENT_REFERENCE_KEYS = (
    'requirement_id', 'task_id', 'task_ids', 'notification_id', 'draft_id',
    'cancellation_id', 'key',
)


def event_summary(event, payload_limit=EVENT_PAYLOAD_PREVIEW_CHARS, *, exact_via=False):
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
    if exact_via:
        summary['exact_via'] = event_exact_via(event['id'])
    return summary


def event_summaries(events, limit, *, exact_via=False):
    """Keep host liveness and the newest input visible under a tight cap."""
    events = [event for event in events or () if event.get('kind') != 'host.stalled']
    if not events:
        return []
    priority = [event for kind in ('host.liveness',)
                for event in events if event['kind'] == kind]
    rest = [event for event in events if event['kind'] != 'host.liveness']
    ordered = priority + rest
    if len(ordered) > limit:
        ordered = ordered[:max(0, limit - 1)] + [events[-1]]
        seen, unique = set(), []
        for event in ordered:
            if event['id'] not in seen:
                seen.add(event['id'])
                unique.append(event)
        ordered = unique
    return [event_summary(event, exact_via=exact_via) for event in ordered]


def host_condition_state(store_path):
    """Read the host's private condition ledger without creating it."""
    from pathlib import Path
    try:
        value = json.loads(Path(str(store_path) + '.host.json').read_text())
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    conditions = value.get('conditions') if isinstance(value, dict) else None
    return conditions if isinstance(conditions, dict) else {}


def conditions_with_state(progression, state=None):
    """Merge stalled progression with durable host backoff metadata."""
    state = state if isinstance(state, dict) else {}
    conditions = conditions_from_progression(progression)
    for condition in conditions:
        saved = state.get(condition['id']) or {}
        for field in ('since', 'generation', 'last_woken_at'):
            if field in saved:
                condition[field] = saved[field]
    return conditions


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
