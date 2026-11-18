"""Strict model-facing tool surface layered over the existing service API."""
from __future__ import annotations

from copy import deepcopy

TEXT = {'type': 'string', 'minLength': 1}
OPTIONAL_TEXT = {'type': 'string'}
BOOLEAN = {'type': 'boolean'}
POSITIVE_INTEGER = {'type': 'integer', 'minimum': 1}
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 200}
HISTORY_LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 100}
STRINGS = {'type': 'array', 'items': TEXT}


_ACTION_TO_V2 = {
    ('ancor_board', 'create'): 'ancor_task_create',
    ('ancor_board', 'list'): 'ancor_task_list',
    ('ancor_board', 'show'): 'ancor_task_show',
    ('ancor_board', 'history'): 'ancor_task_history',
    ('ancor_board', 'block'): 'ancor_task_block',
    ('ancor_board', 'unblock'): 'ancor_task_unblock',
    ('ancor_board', 'revise'): 'ancor_task_revise',
    ('ancor_board', 'comment'): 'ancor_task_comment',
    ('ancor_board', 'link'): 'ancor_task_link',
    ('ancor_board', 'unlink'): 'ancor_task_unlink',
    ('ancor_board', 'archive'): 'ancor_task_archive',
    ('ancor_board', 'associate'): 'ancor_admin_task_associate',
    ('ancor_note', 'list'): 'ancor_note_list',
    ('ancor_note', 'get'): 'ancor_note_get',
    ('ancor_note', 'history'): 'ancor_note_history',
    ('ancor_note', 'add'): 'ancor_note_add',
    ('ancor_note', 'append'): 'ancor_note_add',
    ('ancor_note', 'update'): 'ancor_note_update',
    ('ancor_note', 'replace'): 'ancor_note_update',
    ('ancor_note', 'delete'): 'ancor_note_delete',
    ('ancor_note', 'invalidate'): 'ancor_note_delete',
    ('ancor_wake', 'list'): 'ancor_wake_list',
    ('ancor_wake', 'schedule'): 'ancor_wake_schedule',
    ('ancor_wake', 'replace'): 'ancor_wake_schedule',
    ('ancor_wake', 'cancel'): 'ancor_wake_cancel',
    ('ancor_question', 'list'): 'ancor_question_list',
    ('ancor_question', 'get'): 'ancor_question_get',
    ('ancor_question', 'create'): 'ancor_question_create',
    ('ancor_question', 'withdraw'): 'ancor_question_withdraw',
    ('ancor_change', 'propose'): 'ancor_change_propose',
    ('ancor_change', 'get'): 'ancor_change_get',
    ('ancor_change', 'history'): 'ancor_change_history',
    ('ancor_request', 'draft'): 'ancor_request_draft',
    ('ancor_request', 'confirm'): 'ancor_request_confirm',
    ('ancor_request', 'pause'): 'ancor_request_pause_propose',
    ('ancor_request', 'resume'): 'ancor_request_resume_propose',
    ('ancor_request', 'cancel'): 'ancor_request_cancel_propose',
    ('ancor_request', 'advance'): 'ancor_request_advance',
    ('ancor_request', 'accept'): 'ancor_request_accept_propose',
    ('ancor_request', 'request_changes'): 'ancor_request_changes_propose',
    ('ancor_reconcile', 'get'): 'ancor_impact_get',
    ('ancor_reconcile', 'review'): 'ancor_impact_review',
    ('ancor_reconcile', 'finish'): 'ancor_impact_finish',
    ('ancor_acceptance', 'verify'): 'ancor_delivery_verify',
    ('ancor_acceptance', 'accept'): 'ancor_delivery_verify',
    ('ancor_acceptance', 'reject'): 'ancor_delivery_reject',
    ('ancor_notify', 'list'): 'ancor_notification_list',
    ('ancor_notify', 'respond'): 'ancor_notification_respond',
    ('ancor_signal', 'propose'): 'ancor_signal_propose',
}

_LEGACY_REFERENCE_TOOLS = {
    tool for tool, _action in _ACTION_TO_V2
} | {'ancor_get_context', 'ancor_signal'}
_DROP_REFERENCE = object()

V2_MUTATION_TOOLS = frozenset({
    'ancor_event_ack', 'ancor_request_advance',
    'ancor_note_add', 'ancor_note_update', 'ancor_note_delete',
    'ancor_wake_schedule', 'ancor_wake_cancel',
    'ancor_task_create', 'ancor_task_title_update', 'ancor_task_priority_update',
    'ancor_task_contract_update', 'ancor_task_block', 'ancor_task_unblock',
    'ancor_task_revise', 'ancor_task_comment', 'ancor_task_link',
    'ancor_task_unlink', 'ancor_task_archive',
    'ancor_question_create', 'ancor_question_withdraw',
    'ancor_impact_review', 'ancor_impact_finish',
    'ancor_delivery_verify', 'ancor_delivery_reject', 'ancor_completion_send',
    'ancor_admin_task_associate',
})


def rewrite_legacy_references(value, *, role=None):
    """Rewrite model-facing expand/query references to direct v2 tool names."""
    if isinstance(value, list):
        items = (rewrite_legacy_references(item, role=role) for item in value)
        return [item for item in items if item is not _DROP_REFERENCE]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get('tool'), str) and isinstance(value.get('args'), dict):
        tool = value['tool']
        if tool not in _LEGACY_REFERENCE_TOOLS:
            return value
        args = dict(value['args'])
        action = args.pop('action', None)
        if tool == 'ancor_board' and action == 'edit':
            if 'contract' in args:
                tool = 'ancor_task_contract_update'
            elif 'title' in args:
                tool = 'ancor_task_title_update'
            else:
                tool = 'ancor_task_priority_update'
        elif tool == 'ancor_get_context':
            if 'event_id' in args:
                tool = 'ancor_event_get'
            elif 'request_id' in args:
                tool = 'ancor_request_get'
            else:
                tool = 'ancor_overview'
        elif tool == 'ancor_notify' and action == 'list' and 'id' in args:
            tool = 'ancor_notification_get'
        elif tool in {'ancor_signal', 'ancor_request'} and action is None and tool == 'ancor_signal':
            tool = 'ancor_signal_propose'
        else:
            mapped = _ACTION_TO_V2.get((tool, action))
            if mapped is None and action is not None:
                args['action'] = action
            elif mapped is not None:
                tool = mapped
        if role == 'coordinator' and tool in {
                'ancor_request_accept_propose', 'ancor_request_changes_propose'}:
            return _DROP_REFERENCE
        if tool == 'ancor_request_cancel_propose' and 'request_id' in args:
            args['request_ids'] = [args.pop('request_id')]
        if tool == 'ancor_wake_schedule' and action == 'replace':
            args['replaces_wake_id'] = args.pop('wake_id')
        return {**value, 'tool': tool, 'args': args}
    result = {}
    for key, item in value.items():
        rewritten = rewrite_legacy_references(item, role=role)
        if rewritten is not _DROP_REFERENCE:
            result[key] = rewritten
    return result


def build_v2_surface(*, role, methods, legacy_definitions, contract_schema, admin_tools=False):
    """Return direct v2 definitions and handlers for one ANCOR role.

    Read operations stay grouped by resource. Every mutation has exactly one
    operation and a direct schema. The existing service methods remain the
    single source of business behavior.
    """
    definitions = {}
    handlers = {}

    def add(name, description, properties, required, handler):
        definitions[name] = (description, properties, required)
        handlers[name.removeprefix('ancor_')] = handler

    def delegate(method, payload):
        return lambda args: methods[method]({**payload, **args})

    def board(action, fields=()):
        return lambda args: methods['board']({
            'action': action,
            **{field: args[field] for field in fields if field in args},
        })

    def notify_list(args):
        payload = {'action': 'list', **args}
        return methods['notify'](payload)

    def question(action, fields=()):
        return lambda args: methods['question']({
            'action': action,
            **{field: args[field] for field in fields if field in args},
        })

    def reconcile(action, fields=()):
        return lambda args: methods['reconcile']({
            'action': action,
            **{field: args[field] for field in fields if field in args},
        })

    if role not in {'interactor', 'coordinator'}:
        return definitions, handlers

    # Shared read-only surface.
    add(
        'ancor_overview',
        'Read the compact global ANCOR overview: open requests, attention items, task counts, health and bounded pending-event previews.',
        {}, [], lambda _args: methods['get_context']({}),
    )
    add(
        'ancor_request_get',
        'Read one request and its compact current state. Expand content or history only when exact requirements or audit context are needed.',
        {
            'request_id': TEXT,
            'include_content': BOOLEAN,
            'include_history': BOOLEAN,
            'before_id': POSITIVE_INTEGER,
            'task_limit': LIMIT,
            'notification_limit': LIMIT,
            'attention_limit': {'type': 'integer', 'minimum': 0, 'maximum': 50},
            'include_done': BOOLEAN,
            'include_archived': BOOLEAN,
            'include_closed_notifications': BOOLEAN,
        },
        ['request_id'], lambda args: methods['get_context'](args),
    )
    add(
        'ancor_task_list', 'List linked native task cards with compact status and ownership information.',
        {'request_id': OPTIONAL_TEXT, 'status': OPTIONAL_TEXT, 'limit': LIMIT,
         'include_archived': BOOLEAN},
        [], board('list', ('request_id', 'status', 'limit', 'include_archived')),
    )
    add(
        'ancor_task_show', 'Show one linked native task card with bounded recent history.',
        {'task_id': TEXT}, ['task_id'], board('show', ('task_id',)),
    )
    add(
        'ancor_task_history', 'Expand one bounded category of task history for a linked native card.',
        {'task_id': TEXT, 'kind': {'type': 'string', 'enum': ['comments', 'events', 'runs']},
         'limit': HISTORY_LIMIT, 'before_id': POSITIVE_INTEGER},
        ['task_id', 'kind'], board('history', ('task_id', 'kind', 'limit', 'before_id')),
    )
    change_properties = legacy_definitions['ancor_change'][1]
    add(
        'ancor_change_get', 'Read one committed or pending ANCOR change record without changing it.',
        {'change_id': TEXT}, ['change_id'],
        lambda args: methods['change']({'action': 'get', **args}),
    )
    add(
        'ancor_change_history', 'Read bounded committed ANCOR change history for one request.',
        {'request_id': TEXT, 'before': TEXT, 'limit': LIMIT}, ['request_id'],
        lambda args: methods['change']({'action': 'history', **args}),
    )
    add(
        'ancor_question_list', 'List request questions, open questions by default.',
        {'request_id': OPTIONAL_TEXT, 'include_closed': BOOLEAN, 'limit': LIMIT}, [],
        question('list', ('request_id', 'include_closed', 'limit')),
    )
    add(
        'ancor_question_get', 'Read one question by exact ID, optionally scoped to a request.',
        {'id': TEXT, 'request_id': OPTIONAL_TEXT}, ['id'],
        question('get', ('id', 'request_id')),
    )
    add(
        'ancor_notification_list', 'List compact durable notification and question-delivery records.',
        {'request_id': OPTIONAL_TEXT, 'task_id': OPTIONAL_TEXT, 'state': OPTIONAL_TEXT,
         'open_only': BOOLEAN, 'limit': LIMIT}, [], notify_list,
    )
    add(
        'ancor_notification_get', 'Read one notification or question-delivery record by exact ID.',
        {'id': TEXT}, ['id'],
        lambda args: methods['notify']({'action': 'list', **args}),
    )

    if role == 'interactor':
        request_properties = legacy_definitions['ancor_request'][1]
        add(
            'ancor_request_draft', 'Prepare a new formal request draft and its exact native confirmation.',
            {key: deepcopy(request_properties[key]) for key in
             ('title', 'brief', 'summary', 'replacement_of')},
            ['title', 'brief'], delegate('request', {'action': 'draft'}),
        )
        add(
            'ancor_request_confirm', 'Prepare confirmation for an existing request draft.',
            {'draft_id': TEXT}, ['draft_id'], delegate('request', {'action': 'confirm'}),
        )
        add(
            'ancor_request_pause_propose', 'Prepare a confirmed pause proposal for one stable active request.',
            {'request_id': TEXT, 'reason': TEXT, 'summary': OPTIONAL_TEXT},
            ['request_id', 'reason'], delegate('request', {'action': 'pause'}),
        )
        add(
            'ancor_request_resume_propose', 'Prepare a confirmed resume proposal for one paused request.',
            {'request_id': TEXT, 'reason': TEXT, 'summary': OPTIONAL_TEXT},
            ['request_id', 'reason'], delegate('request', {'action': 'resume'}),
        )
        add(
            'ancor_request_cancel_propose', 'Prepare a confirmed cancellation proposal for one or more requests.',
            {'request_ids': STRINGS, 'reason': OPTIONAL_TEXT, 'summary': OPTIONAL_TEXT},
            ['request_ids'], delegate('request', {'action': 'cancel'}),
        )
        add(
            'ancor_request_accept_propose', 'Prepare the human acceptance decision for the latest verified delivery.',
            {'request_id': TEXT}, ['request_id'], delegate('request', {'action': 'accept'}),
        )
        add(
            'ancor_request_changes_propose', 'Prepare a human request-changes decision with exact feedback.',
            {'request_id': TEXT, 'feedback': TEXT}, ['request_id', 'feedback'],
            delegate('request', {'action': 'request_changes'}),
        )
        add(
            'ancor_change_propose', 'Create one atomic human change proposal and its exact native confirmation.',
            {key: deepcopy(change_properties[key]) for key in
             ('request_id', 'input', 'base', 'operations')},
            ['input', 'operations'], delegate('change', {'action': 'propose'}),
        )
        add(
            'ancor_signal_propose', 'Prepare a confirmed informational signal to the coordinator.',
            {'text': TEXT}, ['text'], delegate('signal', {}),
        )
        add(
            'ancor_notification_respond', 'Prepare an exact native confirmation for answering an open question notification.',
            {'id': TEXT, 'answer': TEXT}, ['id', 'answer'],
            delegate('notify', {'action': 'respond'}),
        )
        return definitions, handlers

    # Coordinator-only reads.
    add(
        'ancor_event_get', 'Read one complete pending ANCOR event and its linked task reference.',
        {'event_id': POSITIVE_INTEGER}, ['event_id'],
        lambda args: methods['get_context'](args),
    )
    add(
        'ancor_note_list', 'List bounded coordinator working-note previews for one request.',
        {'request_id': TEXT, 'limit': LIMIT, 'include_deleted': BOOLEAN}, ['request_id'],
        delegate('note', {'action': 'list'}),
    )
    add(
        'ancor_note_get', 'Read one complete coordinator working note.',
        {'request_id': TEXT, 'note_id': TEXT}, ['request_id', 'note_id'],
        delegate('note', {'action': 'get'}),
    )
    add(
        'ancor_note_history', 'Read retained revisions of one coordinator working note.',
        {'request_id': TEXT, 'note_id': TEXT}, ['request_id', 'note_id'],
        delegate('note', {'action': 'history'}),
    )
    add(
        'ancor_wake_list', 'List durable future coordinator wakes.',
        {'request_id': OPTIONAL_TEXT,
         'state': {'type': 'string', 'enum': ['scheduled', 'queued', 'suppressed', 'cancelled', 'handled']},
         'limit': LIMIT}, [], delegate('wake', {'action': 'list'}),
    )
    add(
        'ancor_impact_get', 'Read current request-change review, task basis and pending impact adjustments.',
        {'request_id': TEXT}, ['request_id'], reconcile('get', ('request_id',)),
    )

    # Coordinator mutations: one direct operation per tool.
    add(
        'ancor_event_ack', 'Acknowledge only event IDs whose consequences have been handled.',
        {'event_ids': {'type': 'array', 'minItems': 1,
                       'items': {'type': 'integer', 'minimum': 1}},
         'reason': OPTIONAL_TEXT}, ['event_ids'], methods['ack_events'],
    )
    add(
        'ancor_request_advance', 'Commit one requested request transition after linked native cards have converged.',
        {'request_id': TEXT}, ['request_id'], delegate('request', {'action': 'advance'}),
    )
    add(
        'ancor_note_add', 'Add one durable coordinator working note to a stable active or paused request.',
        {'request_id': TEXT, 'body': TEXT}, ['request_id', 'body'],
        delegate('note', {'action': 'add'}),
    )
    add(
        'ancor_note_update', 'Replace one note body using its expected revision.',
        {'request_id': TEXT, 'note_id': TEXT, 'body': TEXT, 'expected_revision': POSITIVE_INTEGER},
        ['request_id', 'note_id', 'body', 'expected_revision'],
        delegate('note', {'action': 'update'}),
    )
    add(
        'ancor_note_delete', 'Soft-delete one note using its expected revision.',
        {'request_id': TEXT, 'note_id': TEXT, 'expected_revision': POSITIVE_INTEGER},
        ['request_id', 'note_id', 'expected_revision'],
        delegate('note', {'action': 'delete'}),
    )

    def schedule_wake(args):
        if args.get('replaces_wake_id'):
            return methods['wake']({'action': 'replace', 'wake_id': args['replaces_wake_id'], **args})
        return methods['wake']({'action': 'schedule', **args})

    add(
        'ancor_wake_schedule', 'Schedule a future one-shot wake, optionally replacing one pending wake.',
        {'request_id': TEXT, 'reason': TEXT, 'due_at': {'type': 'number'},
         'replaces_wake_id': OPTIONAL_TEXT},
        ['request_id', 'reason', 'due_at'], schedule_wake,
    )
    add(
        'ancor_wake_cancel', 'Cancel one pending future wake.',
        {'wake_id': TEXT}, ['wake_id'], delegate('wake', {'action': 'cancel'}),
    )
    add(
        'ancor_task_create', 'Create one held native task card with a strict worker contract.',
        {'request_id': TEXT, 'key': TEXT, 'title': TEXT, 'contract': contract_schema,
         'generation': POSITIVE_INTEGER, 'point_versions': STRINGS,
         'parent_ids': STRINGS, 'hold': BOOLEAN, 'priority': {'type': 'integer'},
         'max_runtime_seconds': POSITIVE_INTEGER},
        ['request_id', 'key', 'title', 'contract', 'max_runtime_seconds'],
        board('create', ('request_id', 'key', 'title', 'contract', 'generation', 'point_versions',
                         'parent_ids', 'hold', 'priority', 'max_runtime_seconds')),
    )
    add(
        'ancor_task_title_update', 'Change one linked task title after proving it is quiescent.',
        {'task_id': TEXT, 'title': TEXT}, ['task_id', 'title'], board('edit', ('task_id', 'title')),
    )
    add(
        'ancor_task_priority_update', 'Change one linked task priority.',
        {'task_id': TEXT, 'priority': {'type': 'integer'}}, ['task_id', 'priority'],
        board('edit', ('task_id', 'priority')),
    )
    add(
        'ancor_task_contract_update', 'Replace a stopped task contract using the current request basis.',
        {'task_id': TEXT, 'contract': contract_schema, 'generation': POSITIVE_INTEGER,
         'point_versions': STRINGS}, ['task_id', 'contract'],
        board('edit', ('task_id', 'contract', 'generation', 'point_versions')),
    )
    add(
        'ancor_task_block', 'Block one linked task with an exact reason.',
        {'task_id': TEXT, 'reason': TEXT}, ['task_id', 'reason'], board('block', ('task_id', 'reason')),
    )
    add(
        'ancor_task_unblock', 'Release one linked task after proving quiescence and a stable active request.',
        {'task_id': TEXT, 'reason': TEXT}, ['task_id', 'reason'], board('unblock', ('task_id', 'reason')),
    )
    add(
        'ancor_task_revise', 'Revise one unarchived triage task through the native repair path.',
        {'task_id': TEXT, 'reason': TEXT}, ['task_id', 'reason'], board('revise', ('task_id', 'reason')),
    )
    add(
        'ancor_task_comment', 'Append one audit comment to a linked native task.',
        {'task_id': TEXT, 'body': TEXT}, ['task_id', 'body'], board('comment', ('task_id', 'body')),
    )
    add(
        'ancor_task_link', 'Add a parent-to-child dependency between two linked tasks.',
        {'parent_id': TEXT, 'child_id': TEXT}, ['parent_id', 'child_id'],
        board('link', ('parent_id', 'child_id')),
    )
    add(
        'ancor_task_unlink', 'Remove one parent-to-child dependency between two linked tasks.',
        {'parent_id': TEXT, 'child_id': TEXT}, ['parent_id', 'child_id'],
        board('unlink', ('parent_id', 'child_id')),
    )
    add(
        'ancor_task_archive', 'Archive one quiescent linked task with an exact reason.',
        {'task_id': TEXT, 'reason': TEXT}, ['task_id', 'reason'], board('archive', ('task_id', 'reason')),
    )
    add(
        'ancor_question_create', 'Create one durable coordinator question and request delivery.',
        {'request_id': TEXT, 'id': TEXT, 'body': TEXT, 'reason': TEXT,
         'task_id': OPTIONAL_TEXT, 'point_ids': STRINGS},
        ['request_id', 'id', 'body', 'reason'], question(
            'create', ('request_id', 'id', 'body', 'reason', 'task_id', 'point_ids')),
    )
    add(
        'ancor_question_withdraw', 'Withdraw one obsolete open question with an exact reason.',
        {'id': TEXT, 'reason': TEXT}, ['id', 'reason'], question('withdraw', ('id', 'reason')),
    )

    reconcile_properties = legacy_definitions['ancor_reconcile'][1]
    add(
        'ancor_impact_review', 'Classify the impact of one current request generation on every linked task.',
        {'request_id': TEXT, 'generation': POSITIVE_INTEGER,
         'coverage_reason': TEXT, 'decisions': deepcopy(reconcile_properties['decisions'])},
        ['request_id', 'generation', 'coverage_reason', 'decisions'],
        reconcile('review', ('request_id', 'generation', 'coverage_reason', 'decisions')),
    )
    add(
        'ancor_impact_finish', 'Finish one pending impact adjustment after applying it to its task.',
        {'request_id': TEXT, 'generation': POSITIVE_INTEGER, 'task_id': TEXT, 'evidence': OPTIONAL_TEXT},
        ['request_id', 'generation', 'task_id'],
        reconcile('finish', ('request_id', 'generation', 'task_id', 'evidence')),
    )
    add(
        'ancor_delivery_verify', 'Submit independent coordinator verification for the current request generation.',
        {'request_id': TEXT, 'evidence': deepcopy(legacy_definitions['ancor_acceptance'][1]['evidence'])},
        ['request_id', 'evidence'], delegate('acceptance', {'action': 'verify'}),
    )
    add(
        'ancor_delivery_reject', 'Reject delivery with a concrete reason and keep the request active.',
        {'request_id': TEXT, 'reason': TEXT}, ['request_id', 'reason'],
        delegate('acceptance', {'action': 'reject'}),
    )
    add(
        'ancor_completion_send', 'Enqueue the single completion notice for verified delivery.',
        {'id': TEXT, 'request_id': TEXT, 'message': TEXT}, ['id', 'request_id', 'message'],
        delegate('notify', {'action': 'send', 'kind': 'completion'}),
    )
    if admin_tools:
        add(
            'ancor_admin_task_associate', 'Repair one task-to-request ownership association.',
            {'task_id': TEXT, 'request_id': TEXT, 'reason': TEXT},
            ['task_id', 'request_id', 'reason'], board(
                'associate', ('task_id', 'request_id', 'reason')),
        )
    return definitions, handlers
