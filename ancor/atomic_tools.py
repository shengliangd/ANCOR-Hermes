"""Role-scoped interface to changes, questions, and incremental coordination."""
from .changes import FIELDS, envelope

TEXT = {'type': 'string', 'minLength': 1}
INTEGER = {'type': 'integer', 'minimum': 1}
STRINGS = {'type': 'array', 'items': TEXT}


def interfaces(service, operations, *, interactor, coordinator):
    fields = {'type': 'object', 'properties': {f: STRINGS for f in FIELDS}, 'additionalProperties': False}
    def operation(name, properties, required):
        return {'type': 'object', 'properties': {'op': {'const': name}, **properties},
                'required': ['op', *required], 'additionalProperties': False}
    ops = [
        operation('create', {'title': TEXT, 'fields': fields}, ['title', 'fields']),
        operation('convert', {'fields': fields}, ['fields']),
        operation('append', {'field': {'enum': list(FIELDS)}, 'body': TEXT}, ['field', 'body']),
        operation('replace', {'version_id': TEXT, 'field': {'enum': list(FIELDS)}, 'body': TEXT}, ['version_id', 'body']),
        operation('invalidate', {'version_id': TEXT}, ['version_id']),
        operation('title', {'title': TEXT}, ['title']),
        operation('reply', {'question_id': TEXT, 'revision': INTEGER, 'body': TEXT}, ['question_id', 'revision', 'body']),
        operation('resolve_question', {'question_id': TEXT, 'revision': INTEGER, 'reason': TEXT}, ['question_id', 'revision', 'reason']),
        *[operation(a, {'reason': TEXT}, ['reason']) for a in ('pause', 'resume', 'cancel')],
        operation('accept', {'verification_id': INTEGER}, ['verification_id']),
        operation('request_changes', {'feedback': TEXT}, ['feedback']),
        operation('grant', {'action': TEXT, 'target': TEXT, 'scope': TEXT}, ['action', 'target', 'scope']),
        operation('revoke', {'grant_id': TEXT}, ['grant_id']),
    ]
    definitions = {
        'ancor_change': (
            'Create an immutable atomic change proposal, inspect it, or page committed history. '
            'Use this for structured creation, in-place point changes, answers, lifecycle decisions and grants. '
            'Propose requires exact original input and current base versions from requirement detail; '
            'only exact native clarify confirmation commits. Present the returned human-readable confirmation unchanged; '
            'do not narrate operation JSON, base versions or internal IDs to the user. '
            'Title changes do not alter content generation. '
            'Arrays are unordered semantically. Never invent requirements or authorization. '
            'A reply alone does not change requirements; include point operations and resolve_question as appropriate. '
            'Legacy briefs require explicit convert with complete fields. Coordinator can only read history/get.',
            {'action': {'enum': ['propose', 'get', 'history'] if interactor else ['get', 'history']},
             'requirement_id': TEXT, 'change_id': TEXT, 'input': TEXT,
             'base': {'type': 'object', 'properties': {v: {'type': 'integer', 'minimum': 0}
                      for v in ('generation', 'title_version', 'control_version', 'notes_version')}, 'additionalProperties': False},
             'operations': {'type': 'array', 'minItems': 1, 'maxItems': 200, 'items': {'oneOf': ops}},
             'before': TEXT, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['action']),
        'ancor_question': (
            'Read durable questions, or create/withdraw one as coordinator. Questions may be requirement-scoped '
            'without a task; a linked task must be blocked. Creation durably requests notification delivery. '
            'Reply and resolve through a confirmed ancor_change; replies need not resolve a question. '
            'Notification delivery is separate from resolution.',
            {'action': {'enum': ['list', 'get', 'create', 'withdraw'] if coordinator else ['list', 'get']},
             'requirement_id': TEXT, 'id': TEXT, 'body': TEXT, 'reason': TEXT,
             'task_id': TEXT, 'point_ids': STRINGS, 'include_closed': {'type': 'boolean'},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['action']),
    }

    def change(args, **_kwargs):
        if args['action'] == 'propose' and not interactor:
            raise PermissionError('only interactor may propose human changes')
        if args['action'] == 'get' and coordinator:
            return service.store.get_change(args['change_id'])
        if args['action'] == 'history':
            result = service.change(args)
            result['items'] = [{'id': c['id'], 'requirement_id': c['requirement_id'], 'actor': c['actor'],
                'committed_at': c['committed_at'], 'operations_count': len(c['operations']),
                'kinds': sorted({op['op'] for op in c['operations']}),
                'exact_via': {'tool': 'ancor_change', 'args': {'action': 'get', 'change_id': c['id']}}}
                for c in result['items']]
            return result
        return service.change(args)

    def question(args, **_kwargs):
        store = service.store
        action = args['action']
        if action in {'create', 'withdraw'} and not coordinator:
            raise PermissionError('coordinator required')
        if action == 'create':
            if args.get('task_id'):
                task = operations.board.show(args['task_id'])
                if task['status'] != 'blocked':
                    raise ValueError('question task must be blocked')
            return store.create_question(args['requirement_id'], identifier=args['id'], body=args['body'],
                                         reason=args['reason'], task_id=args.get('task_id'),
                                         point_ids=args.get('point_ids', []), actor=service.ctx.profile_name)
        if action == 'withdraw':
            return store.withdraw_question(args['id'], args['reason'], service.ctx.profile_name)
        rows = store.questions(args.get('requirement_id'))
        if action == 'get':
            for row in rows:
                if row['id'] == args['id']:
                    return row
            raise KeyError(args['id'])
        if action != 'list':
            raise ValueError('unknown question action')
        if not args.get('include_closed', False):
            rows = [r for r in rows if r['state'] == 'open']
        limit = args.get('limit', 50)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit must be 1..200')
        return envelope([question_ref(r) for r in rows[:limit]], len(rows))

    methods = {'change': change, 'question': question}
    if coordinator:
        definitions['ancor_reconcile'] = (
            'Review current generation against EVERY linked task, including finished work, and explain coverage '
            'of new requirements. Decisions: unaffected, reverify, revise, retire. A requirement change never '
            'automatically stops all cards. Finish each pending adjustment only after applying it; revise requires '
            'a current task basis and a quiescent worker. Reverify requires explicit evidence. '
            'Get returns durable review/basis state even after wake events were acknowledged.',
            {'action': {'enum': ['get', 'review', 'finish']}, 'requirement_id': TEXT,
             'generation': INTEGER, 'task_id': TEXT, 'evidence': TEXT, 'coverage_reason': TEXT,
             'decisions': {'type': 'array', 'items': {'type': 'object', 'properties': {
                 'task_id': TEXT, 'decision': {'enum': ['unaffected', 'reverify', 'revise', 'retire']},
                 'reason': TEXT}, 'required': ['task_id', 'decision', 'reason'], 'additionalProperties': False}}},
            ['action', 'requirement_id'])

        def reconcile(args, **_kwargs):
            store, rid = service.store, args['requirement_id']
            if args['action'] == 'get':
                return store.coordination(rid)
            operations.reconcile()
            if args['action'] == 'review':
                return store.review_impact(rid, args['generation'], args['decisions'], args['coverage_reason'])
            if args['action'] != 'finish':
                raise ValueError('unknown reconciliation action')
            state = store.coordination(rid)
            tid = args['task_id']
            review = next((r for r in state['reviews'] if r['task_id'] == tid), None)
            if not review:
                raise ValueError('task has no current review')
            detail = operations.detail(tid)
            if review['decision'] == 'retire':
                if detail['task']['status'] not in {'archived', 'cancelled'}:
                    raise ValueError('retired task must be archived or cancelled')
                operations.require_quiescence(detail)
            elif review['decision'] == 'revise':
                operations.require_quiescence(detail)
                basis = next((b for b in state['task_basis'] if b['task_id'] == tid), None)
                if not basis or basis['generation'] != args['generation']:
                    raise ValueError('revised card needs current basis')
            elif review['decision'] == 'reverify':
                store._text(args.get('evidence'))
            store.finish_impact(rid, args['generation'], tid, args.get('evidence'))
            return store.coordination(rid)
        methods['reconcile'] = reconcile
    return definitions, methods


def question_ref(row):
    return {k: row[k] for k in ('id', 'requirement_id', 'task_id', 'state', 'revision')}
