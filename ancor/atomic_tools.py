"""Role-scoped interface to atomic changes and durable questions."""
from .changes import envelope
from .request_contract import DESCRIPTIONS, FIELDS

TEXT = {'type': 'string', 'minLength': 1}
INTEGER = {'type': 'integer', 'minimum': 1}
STRINGS = {'type': 'array', 'items': TEXT}


def interfaces(service, *, interactor, coordinator):
    fields = {
        'type': 'object',
        'properties': {
            field: {**STRINGS, 'description': DESCRIPTIONS[field]} for field in FIELDS
        },
        'additionalProperties': False,
    }

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
            'Propose requires exact original input and current base versions from request detail; '
            'only exact native clarify confirmation commits.',
            {'action': {'enum': ['propose', 'get', 'history'] if interactor else ['get', 'history']},
             'request_id': TEXT, 'change_id': TEXT, 'input': TEXT,
             'base': {'type': 'object', 'properties': {v: {'type': 'integer', 'minimum': 0}
                      for v in ('generation', 'title_version', 'control_version', 'notes_version')}, 'additionalProperties': False},
             'operations': {'type': 'array', 'minItems': 1, 'maxItems': 200, 'items': {'oneOf': ops}},
             'before': TEXT, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['action']),
        'ancor_question': (
            'Read durable questions, or create/withdraw one as Root Agent. '
            'Creation durably requests notification delivery. '
            'Reply and resolve through a confirmed ancor_change.',
            {'action': {'enum': ['list', 'get', 'create', 'withdraw'] if coordinator else ['list', 'get']},
             'request_id': TEXT, 'id': TEXT, 'body': TEXT, 'reason': TEXT,
             'point_ids': STRINGS, 'include_closed': {'type': 'boolean'},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['action']),
    }

    def change(args, **_kwargs):
        if args['action'] == 'propose' and not interactor:
            raise PermissionError('only interactor may propose human changes')
        if args['action'] == 'get' and coordinator:
            return service.store.get_change(args['change_id'])
        if args['action'] == 'history':
            result = service.change(args)
            result['items'] = [{
                'id': c['id'], 'request_id': c['request_id'], 'actor': c['actor'],
                'committed_at': c['committed_at'], 'operations_count': len(c['operations']),
                'kinds': sorted({op['op'] for op in c['operations']}),
                'exact_via': {'tool': 'ancor_change_get', 'args': {'change_id': c['id']}},
            } for c in result['items']]
            return result
        return service.change(args)

    def question(args, **_kwargs):
        store = service.store
        action = args['action']
        if action in {'create', 'withdraw'} and not coordinator:
            raise PermissionError('Root Agent profile required')
        if action == 'create':
            return store.create_question(
                args['request_id'], identifier=args['id'], body=args['body'],
                reason=args['reason'], point_ids=args.get('point_ids', []),
                actor=service.ctx.profile_name)
        if action == 'withdraw':
            return store.withdraw_question(args['id'], args['reason'], service.ctx.profile_name)
        rows = store.questions(args.get('request_id'))
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

    return definitions, {'change': change, 'question': question}


def question_ref(row):
    return {k: row[k] for k in ('id', 'request_id', 'state', 'revision')}
