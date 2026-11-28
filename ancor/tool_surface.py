"""Strict role-scoped tool surface for Interactor (default) and ancor-exec (root | goal | judge)."""
from __future__ import annotations

from copy import deepcopy

TEXT = {'type': 'string', 'minLength': 1}
OPTIONAL_TEXT = {'type': 'string'}
BOOLEAN = {'type': 'boolean'}
POSITIVE_INTEGER = {'type': 'integer', 'minimum': 1}
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 200}
STRINGS = {'type': 'array', 'items': TEXT}

ARTIFACT_SCHEMA = {
    'type': 'object',
    'properties': {'path': TEXT, 'desc': TEXT},
    'required': ['path', 'desc'],
    'additionalProperties': False,
}

GENERIC_BUDGET_SCHEMA = {
    'type': 'object',
    'description': (
        'Generic 3-layer resource budget: limits (arbitrary numeric keys enforced monotonically), '
        'allowed (arbitrary string-list allowlists enforced as subsets), and constraints '
        '(natural-language soft boundaries auto-inherited by child goals).'
    ),
    'properties': {
        'limits': {
            'type': 'object',
            'additionalProperties': {'type': 'number', 'minimum': 0},
        },
        'allowed': {
            'type': 'object',
            'additionalProperties': STRINGS,
        },
        'constraints': STRINGS,
    },
    'additionalProperties': False,
}

GOAL_SPEC_PROPERTIES = {
    'goal_yaml': OPTIONAL_TEXT,
    'goal_id': OPTIONAL_TEXT,
    'request_ref': {
        'type': 'object',
        'properties': {'id': TEXT, 'generation': POSITIVE_INTEGER},
        'required': ['id', 'generation'],
        'additionalProperties': False,
    },
    'objective': OPTIONAL_TEXT,
    'done_criteria': OPTIONAL_TEXT,
    'scope': {
        'type': 'object',
        'properties': {
            'readable_requests': STRINGS,
            'writable_paths': STRINGS,
            'can_ask_human': BOOLEAN,
        },
        'additionalProperties': False,
    },
    'budget': GENERIC_BUDGET_SCHEMA,
    'category': {
        'type': 'string',
        'description': ('Optional semantic model category (an alias name from ancor-exec model_aliases, '
                        'e.g. research / coding / fast; see model_categories in your packet). '
                        'Omit to use the ancor-exec default model. Unknown names are rejected.'),
    },
}

VERIFICATION_EVIDENCE_SCHEMA = {
    'type': 'object',
    'properties': {
        'generation': POSITIVE_INTEGER,
        'coverage': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'version_id': TEXT,
                    'satisfied': BOOLEAN,
                    'evidence': TEXT,
                },
                'required': ['version_id', 'satisfied', 'evidence'],
                'additionalProperties': False,
            },
        },
        'verification': {'type': 'array', 'minItems': 1, 'items': TEXT},
        'artifacts': {'type': 'array', 'minItems': 1, 'items': TEXT},
    },
    'required': ['generation', 'coverage', 'verification', 'artifacts'],
    'additionalProperties': False,
}

ROOT_MUTATION_TOOLS = frozenset({
    'ancor_event_ack', 'ancor_request_advance',
    'ancor_note_add', 'ancor_note_update', 'ancor_note_delete',
    'ancor_question_create', 'ancor_question_withdraw',
    'ancor_completion_send',
    'ancor_goal_spawn', 'ancor_goal_signal',
    'ancor_goal_pause', 'ancor_goal_resume', 'ancor_goal_stop',
    'ancor_goal_update_budget',
    'ancor_delivery_submit',
})


def build_surface(*, role: str, methods: dict, change_properties: dict):
    """Return direct tool definitions and handlers for one ANCOR role."""
    definitions = {}
    handlers = {}

    def add(name, description, properties, required, handler):
        definitions[name] = (description, properties, required)
        handlers[name.removeprefix('ancor_')] = handler

    def delegate(method, payload):
        return lambda args: methods[method]({**payload, **args})

    def question(action, fields=()):
        return lambda args: methods['question']({
            'action': action,
            **{field: args[field] for field in fields if field in args},
        })

    if role == 'goal':
        add(
            'ancor_request_read',
            'Read authoritative requirement text directly from DB for a request in your scope.readable_requests.',
            {'request_id': TEXT}, ['request_id'],
            methods['goal_read_request'],
        )
        add(
            'ancor_goal_self',
            'Inspect your own active /goal contract, generic budget, children, and active resource handles.',
            {}, [],
            methods['goal_self'],
        )
        add(
            'ancor_goal_spawn',
            'Spawn a child /goal with generic monotonic attenuation (limits, allowed, inherited constraints).',
            deepcopy(GOAL_SPEC_PROPERTIES), [],
            methods['goal_spawn'],
        )
        add(
            'ancor_goal_signal',
            'Send a steer or update_generation control signal to one of your descendant goals.',
            {'goal_id': TEXT, 'kind': {'type': 'string', 'enum': ['steer', 'update_generation']},
             'message': OPTIONAL_TEXT, 'generation': POSITIVE_INTEGER},
            ['goal_id', 'kind'],
            methods['goal_signal'],
        )
        add(
            'ancor_goal_pause',
            'Deterministically pause a descendant goal subtree (drain keeps external jobs, preempt cancels them).',
            {'goal_id': TEXT, 'mode': {'type': 'string', 'enum': ['drain', 'preempt']}},
            ['goal_id'],
            methods['goal_pause'],
        )
        add(
            'ancor_goal_resume',
            'Deterministically resume a paused descendant goal subtree.',
            {'goal_id': TEXT}, ['goal_id'],
            methods['goal_resume'],
        )
        add(
            'ancor_goal_stop',
            'Deterministically cancel a descendant goal subtree, killing local processes and external handles.',
            {'goal_id': TEXT, 'reason': OPTIONAL_TEXT}, ['goal_id'],
            methods['goal_stop'],
        )
        add(
            'ancor_goal_report',
            'Submit your structured GoalResult receipt (completed, blocked, infeasible, or cancelled) to your parent.',
            {
                'request_generation': POSITIVE_INTEGER,
                'status': {'type': 'string', 'enum': ['completed', 'blocked', 'infeasible', 'cancelled']},
                'summary': TEXT,
                'artifacts': {'type': 'array', 'items': ARTIFACT_SCHEMA},
                'metrics': {'type': 'object'},
                'open_issues': STRINGS,
            },
            ['request_generation', 'status', 'summary'],
            methods['goal_report'],
        )
        add(
            'ancor_goal_wait',
            'Park this running /goal in waiting_external until wake_after_seconds, wake_at, or watch_path exists (zero LLM cost). '
            'Use this instead of sleeping inside a session for long-running external jobs.',
            {
                'wake_after_seconds': {'type': 'number', 'exclusiveMinimum': 0},
                'wake_at': {'type': 'number', 'exclusiveMinimum': 0},
                'watch_path': OPTIONAL_TEXT,
                'reason': OPTIONAL_TEXT,
            },
            [],
            methods['goal_wait'],
        )
        add(
            'ancor_goal_update_budget',
            'Update the 3-layer budget of a non-terminal child /goal within your own budget limits.',
            {
                'goal_id': TEXT,
                'budget': GENERIC_BUDGET_SCHEMA,
                'reason': OPTIONAL_TEXT,
            },
            ['goal_id', 'budget'],
            methods['goal_update_budget'],
        )
        add(
            'ancor_resource_acquire',
            'Acquire tracked external resources/jobs through the generic GOAL_ID QuotaGuard (checks limits & allowed).',
            {
                'usage': {'type': 'object', 'additionalProperties': {'type': 'number', 'minimum': 0}},
                'tags': {'type': 'object', 'additionalProperties': TEXT},
                'handle_id': OPTIONAL_TEXT,
                'command': OPTIONAL_TEXT,
                'metadata': {'type': 'object'},
            },
            [],
            methods['resource_acquire'],
        )
        add(
            'ancor_resource_release',
            'Release one (or all, when handle_id is omitted) tracked external resource handles owned by this /goal (goal_id optional for goal role, required for root).',
            {
                'goal_id': OPTIONAL_TEXT,
                'handle_id': OPTIONAL_TEXT,
                'status': {'type': 'string', 'enum': ['released', 'succeeded', 'failed', 'cancelled']},
                'detail': OPTIONAL_TEXT,
            },
            [],
            methods['resource_release'],
        )
        return definitions, handlers

    if role == 'judge':
        add(
            'ancor_request_read',
            'Read complete authoritative requirement text and active grants for the request under delivery review.',
            {'request_id': TEXT}, ['request_id'],
            methods['judge_read_request'],
        )
        add(
            'ancor_judge_verdict',
            'Record the independent Judge Final Decision: accept (moves to awaiting_acceptance), '
            'reject (returns structured feedback to Root Agent), or escalate (asks human via Interactor).',
            {
                'delivery_event_id': POSITIVE_INTEGER,
                'request_id': TEXT,
                'generation': POSITIVE_INTEGER,
                'decision': {'type': 'string', 'enum': ['accept', 'reject', 'escalate']},
                'summary': TEXT,
                'evidence': VERIFICATION_EVIDENCE_SCHEMA,
                'feedback': STRINGS,
                'artifacts': {'type': 'array', 'items': ARTIFACT_SCHEMA},
            },
            ['request_id', 'generation', 'decision', 'summary', 'delivery_event_id'],
            methods['judge_verdict'],
        )
        return definitions, handlers

    if role not in {'interactor', 'root'}:
        return definitions, handlers

    # Shared read-only surface for Interactor (default) and Root Agent (ancor-exec ANCOR_ROLE=root).
    add(
        'ancor_overview',
        'Read the compact global ANCOR overview: open requests, active goals, resource usage, notifications and pending events.',
        {}, [], lambda _args: methods['get_context']({}),
    )
    add(
        'ancor_request_get',
        'Read one request and its current state, active goals, and judge verdicts. Expand content or history when needed.',
        {
            'request_id': TEXT,
            'include_content': BOOLEAN,
            'include_history': BOOLEAN,
            'before_id': POSITIVE_INTEGER,
            'notification_limit': LIMIT,
            'include_closed_notifications': BOOLEAN,
        },
        ['request_id'], lambda args: methods['get_context'](args),
    )
    add(
        'ancor_visualize',
        'Generate a read-only visualization (text tree and optional self-contained HTML snapshot) directly from requirements.db.',
        {'request_id': OPTIONAL_TEXT, 'output_path': OPTIONAL_TEXT}, [],
        methods['visualize'],
    )
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
        {'request_id': OPTIONAL_TEXT, 'state': OPTIONAL_TEXT,
         'open_only': BOOLEAN, 'limit': LIMIT}, [],
        lambda args: methods['notify']({'action': 'list', **args}),
    )
    add(
        'ancor_notification_get', 'Read one notification or question-delivery record by exact ID.',
        {'id': TEXT}, ['id'],
        lambda args: methods['notify']({'action': 'list', **args}),
    )

    if role == 'interactor':
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
            'ancor_change_propose',
            'Create one immutable structured request change and its exact native confirmation.',
            {key: deepcopy(change_properties[key]) for key in
             ('request_id', 'input', 'base', 'operations')},
            ['input', 'operations'], delegate('change', {'action': 'propose'}),
        )
        add(
            'ancor_signal_propose', 'Prepare a confirmed informational signal to the Root Agent.',
            {'text': TEXT}, ['text'], delegate('signal', {}),
        )
        add(
            'ancor_notification_respond', 'Prepare an exact native confirmation for answering an open question notification.',
            {'id': TEXT, 'answer': TEXT}, ['id', 'answer'],
            delegate('notify', {'action': 'respond'}),
        )
        return definitions, handlers

    # Root Agent (ancor-exec with ANCOR_ROLE=root): request-level orchestrator, zero code/training tools.
    add(
        'ancor_event_get', 'Read one complete pending ANCOR event.',
        {'event_id': POSITIVE_INTEGER}, ['event_id'],
        lambda args: methods['get_context'](args),
    )
    add(
        'ancor_note_list', 'List bounded Root Agent working-note previews for one request.',
        {'request_id': TEXT, 'limit': LIMIT, 'include_deleted': BOOLEAN}, ['request_id'],
        delegate('note', {'action': 'list'}),
    )
    add(
        'ancor_note_get', 'Read one complete Root Agent working note.',
        {'request_id': TEXT, 'note_id': TEXT}, ['request_id', 'note_id'],
        delegate('note', {'action': 'get'}),
    )
    add(
        'ancor_note_history', 'Read retained revisions of one Root Agent working note.',
        {'request_id': TEXT, 'note_id': TEXT}, ['request_id', 'note_id'],
        delegate('note', {'action': 'history'}),
    )
    add(
        'ancor_goal_list', 'List active (or all) /goal nodes in the execution tree.',
        {'request_id': OPTIONAL_TEXT, 'parent_goal_id': OPTIONAL_TEXT,
         'status': OPTIONAL_TEXT, 'include_terminal': BOOLEAN}, [],
        methods['goal_list'],
    )
    add(
        'ancor_goal_get', 'Read one /goal node, its subtree, active handles, and latest GoalResult.',
        {'goal_id': TEXT}, ['goal_id'],
        methods['goal_get'],
    )
    add(
        'ancor_cluster_status', 'Inspect global active resource utilization across all active goal subtrees.',
        {}, [],
        methods['cluster_status'],
    )
    add(
        'ancor_event_ack', 'Acknowledge only event IDs whose consequences have been handled.',
        {'event_ids': {'type': 'array', 'minItems': 1, 'items': POSITIVE_INTEGER},
         'reason': OPTIONAL_TEXT}, ['event_ids'], methods['ack_events'],
    )
    add(
        'ancor_request_advance', 'Cascade lifecycle down L1 goal subtrees and commit a requested request transition.',
        {'request_id': TEXT}, ['request_id'], delegate('request', {'action': 'advance'}),
    )
    add(
        'ancor_note_add', 'Add one durable working note to a stable active or paused request.',
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
    add(
        'ancor_question_create', 'Create one durable question for the human (via Interactor).',
        {'request_id': TEXT, 'id': TEXT, 'body': TEXT, 'reason': TEXT, 'point_ids': STRINGS},
        ['request_id', 'id', 'body', 'reason'],
        question('create', ('request_id', 'id', 'body', 'reason', 'point_ids')),
    )
    add(
        'ancor_question_withdraw', 'Withdraw one obsolete open question with an exact reason.',
        {'id': TEXT, 'reason': TEXT}, ['id', 'reason'],
        question('withdraw', ('id', 'reason')),
    )
    add(
        'ancor_completion_send', 'Enqueue the single completion notice for a Judge-verified delivery.',
        {'id': TEXT, 'request_id': TEXT, 'message': TEXT}, ['id', 'request_id', 'message'],
        delegate('notify', {'action': 'send', 'kind': 'completion'}),
    )
    add(
        'ancor_goal_spawn', 'Spawn a new L1 /goal with generic 3-layer budget (limits, allowed, constraints).',
        deepcopy(GOAL_SPEC_PROPERTIES), [],
        methods['goal_spawn'],
    )
    add(
        'ancor_goal_signal',
        'Send a steer or update_generation control signal to an active goal (lower agent reads authoritative DB directly).',
        {'goal_id': TEXT, 'kind': {'type': 'string', 'enum': ['steer', 'update_generation']},
         'message': OPTIONAL_TEXT, 'generation': POSITIVE_INTEGER},
        ['goal_id', 'kind'],
        methods['goal_signal'],
    )
    add(
        'ancor_goal_pause',
        'Deterministically pause a goal subtree (drain keeps external jobs running; preempt cancels them).',
        {'goal_id': TEXT, 'mode': {'type': 'string', 'enum': ['drain', 'preempt']}},
        ['goal_id'],
        methods['goal_pause'],
    )
    add(
        'ancor_goal_resume',
        'Deterministically resume a paused goal subtree (skips independently paused or terminal descendants).',
        {'goal_id': TEXT}, ['goal_id'],
        methods['goal_resume'],
    )
    add(
        'ancor_goal_stop',
        'Deterministically cancel a goal subtree, terminating local PIDs and cancelling external handles.',
        {'goal_id': TEXT, 'reason': OPTIONAL_TEXT}, ['goal_id'],
        methods['goal_stop'],
    )
    add(
        'ancor_goal_update_budget',
        'Dynamically update a non-terminal goal budget (e.g. tighten lower-priority limits so higher-priority work can proceed).',
        {'goal_id': TEXT, 'budget': GENERIC_BUDGET_SCHEMA, 'reason': OPTIONAL_TEXT},
        ['goal_id', 'budget'],
        methods['goal_update_budget'],
    )
    add(
        'ancor_resource_release',
        'Release one (or all, when handle_id is omitted) tracked external resource handles on a goal (goal_id required for root, optional for goal role).',
        {
            'goal_id': OPTIONAL_TEXT,
            'handle_id': OPTIONAL_TEXT,
            'status': {'type': 'string', 'enum': ['released', 'succeeded', 'failed', 'cancelled']},
            'detail': OPTIONAL_TEXT,
        },
        [],
        methods['resource_release'],
    )
    add(
        'ancor_delivery_submit',
        'Submit a request delivery package to the independent Judge Agent (fresh clean context).',
        {
            'request_id': TEXT,
            'summary': TEXT,
            'artifacts': {'type': 'array', 'minItems': 1, 'items': ARTIFACT_SCHEMA},
            'eval_evidence': OPTIONAL_TEXT,
        },
        ['request_id', 'summary', 'artifacts'],
        methods['delivery_submit'],
    )
    return definitions, handlers
