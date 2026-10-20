"""Thin profile-scoped Hermes integration. No provisioning during registration."""
import json
import logging
import os

log = logging.getLogger(__name__)


def register(ctx, *, scan_runner=None):
    if scan_runner is None:
        from .host import scan_runner
    if ctx.profile_name == ctx.get_config('owner_profile', 'default') and ctx.get_config('gateway_owner', False):
        pump = None
        def connected(native, adapter):
            nonlocal pump
            # Public spawn_task returns an asyncio.Task; reconnect must not fork scans.
            if pump is None or pump.done():
                pump = ctx.spawn_task(scan_runner(ctx, adapter), name='ancor:scan')
        ctx.register_platform_handler('api_server', connected)
    interactor_profile = ctx.get_config('interactor_profile', 'ancor-interactor')
    coordinator_profile = ctx.get_config('coordinator_profile', 'ancor-coordinator')
    interactor = ctx.profile_name == interactor_profile
    coordinator = ctx.profile_name == coordinator_profile
    worker = ctx.profile_name == ctx.get_config('worker_profile', 'ancor-worker')
    if not interactor and not coordinator and not worker:
        return
    from .service import Service
    service = Service(ctx)
    from .operations import Operations
    operations = Operations(ctx)
    # One lazy store for all registered business handlers.
    operations.service = service

    def outbox():
        from .notification import HermesSender, NotificationOutbox
        # Fail closed: destination and credential-owning profile are explicit.
        return NotificationOutbox(
            service.store.path + '.notifications.db',
            coordinator_profile=coordinator_profile,
            target=ctx.get_config('human_target'),
            sender=HermesSender(ctx.get_config('transport_profile'),
                                executable=ctx.get_config('sender_executable', 'hermes')))

    def response_event(row):
        return service.store.add_event('notification.response', {
            'notification_id': row['id'], 'body': row['answer'],
            'requirement_id': row.get('requirement_id'), 'task_id': row.get('task_id'),
            'reference': row['answer_reference'],
            'source': 'interactor_transcription', 'authorization': False},
            source_key='notification.response:' + row['id'])

    def notifications():
        rows = outbox().list()
        # Idempotent input ingestion, not a coordination decision.
        for row in rows:
            if row['answer'] is not None:
                response_event(row)
        return rows

    def wake(args):
        from .scheduling import Scheduling
        return Scheduling(service.store).call(args)

    def validate_acks(event_ids, snapshot, tasks):
        pending = {event['id']: event for event in snapshot['pending_events']}
        task_by_id = {task['id']: task for task in tasks or ()}
        for identifier in event_ids:
            event = pending.get(identifier)
            if event is None:
                continue
            if event['kind'] == 'host.stalled':
                raise ValueError(f'event {identifier} is host-owned and clears only when the requirement recovers')
            payload = event.get('payload') or {}
            task_id = payload.get('task_id')
            task = task_by_id.get(task_id, {})
            if event['kind'] == 'native.block_loop_detected' and task.get('status') == 'triage':
                raise ValueError(f'event {identifier} cannot be acknowledged while {task_id} is in triage')

    def get_context(args):
        try:
            extra = {'notifications': notifications()}
        except ValueError as exc:
            extra = {'notifications': [], 'notifications_error': str(exc)}
        tasks = None
        if coordinator:
            try:
                tasks = operations.board.list()
            except Exception as exc:
                extra['board_error'] = str(exc)
        if coordinator and args.get('ack_event_ids'):
            validate_acks(args['ack_event_ids'], service.store.snapshot(), tasks)
        from .scheduling import Scheduling
        data = service.get_context(args)
        return {**data, **Scheduling(service.store).observation(data, tasks, extra['notifications']), **extra}

    def notify(args, **native):
        action = args['action']
        if action == 'list':
            return {'notifications': notifications()}
        box = outbox()
        identifier = args['id']
        if action == 'send':
            if not coordinator:
                raise PermissionError('coordinator profile required to send notifications')
            requirement_id, task_id = args.get('requirement_id'), args.get('task_id')
            if args['kind'] == 'completion':
                req = service.store.get_requirement(requirement_id)
                if req['status'] != 'accepted':
                    raise ValueError('completion requires an accepted requirement')
                if identifier != 'accept:' + req['id']:
                    raise ValueError('completion id must be accept:<requirement_id>')
            if args['kind'] == 'question':
                if not task_id:
                    raise ValueError('question requires requirement_id and task_id')
                req = service.store.get_requirement(requirement_id)
                if req['status'] != 'active':
                    raise ValueError('question requires an active requirement')
                links = {link['task_id'] for link in service.store.snapshot()['task_links']
                         if link['requirement_id'] == req['id']}
                if task_id not in links:
                    raise ValueError('question task must be linked to the requirement')
            box.enqueue(identifier, args['kind'], args['message'], actor=ctx.profile_name,
                        requirement_id=requirement_id, task_id=task_id)
            return box.deliver(identifier, actor=ctx.profile_name)
        if action == 'respond':
            existing = box.get(identifier)
            # IDs come only from handler metadata, never model-supplied provenance.
            # Even native IDs link the transcription; they do not authenticate it.
            reference = json.dumps({key: native[key] for key in ('session_id', 'tool_call_id')
                                    if isinstance(native.get(key), str) and native[key]}, sort_keys=True)
            if reference == '{}':
                reference = 'interactor_transcription:' + identifier
            if existing['answer'] == args['answer']:
                reference = existing['answer_reference']
            if not interactor:
                raise PermissionError('interactor profile required to respond')
            row = box.respond(identifier, args['answer'], reference)
            return {'notification': row, 'event': response_event(row)}
        raise ValueError('unknown notification action')

    def requirement(args):
        if args['action'] != 'respond':
            return service.requirement(args)
        req = service.store.get_requirement(args['requirement_id'])
        body = service.store._text(args['body'])
        # Foreground transcription is advisory input, never consent evidence.
        return service.store.add_event('requirement.response', {
            'requirement_id': req['id'], 'body': body,
            'source': 'interactor_transcription', 'authorization': False})

    def retirement():
        from .retirement import Retirement
        return Retirement(ctx, service.store, operations.board)

    def board_action(args):
        if args['action'] == 'retire':
            return retirement().retire(args['requirement_id'], args['task_id'], args['reason'])
        return operations.call(args)

    def acceptance(args):
        if args['action'] == 'accept':
            operations.reconcile()
            retired = retirement().effective_retired(args['requirement_id'])
            links = [link['task_id'] for link in service.store.snapshot()['task_links']
                     if link['requirement_id'] == args['requirement_id'] and link['task_id'] not in retired]
            if not links or any(operations.board.show(tid)['status'] != 'done' for tid in links):
                raise ValueError('all linked native tasks must be done before acceptance')
        return service.acceptance(args)

    methods = {'requirement': requirement, 'get_context': get_context, 'notify': notify,
               'acceptance': acceptance, 'board': board_action, 'wake': wake}
    if interactor:
        ctx.register_hook('pre_tool_call', service.pre_tool_call)
        ctx.register_hook('post_tool_call', service.post_tool_call)
    definitions = {
        'ancor_wake': ('Manage durable future one-shot wakes. schedule requires requirement_id, reason, due_at (Unix seconds); replace requires wake_id, reason, due_at; cancel requires wake_id. list optionally filters requirement_id. Mutations require coordinator. Never create immediate self-wake loops.',
                       {'action': {'type': 'string', 'enum': ['schedule', 'list', 'cancel', 'replace']},
                        'requirement_id': {'type': 'string'}, 'wake_id': {'type': 'string'},
                        'reason': {'type': 'string'}, 'due_at': {'type': 'number'}}, ['action']),
        'ancor_notify': ('Send a durable question or accepted completion (coordinator only), list notifications, or transcribe a human answer. Answers are advisory, never authorization. Reuse stable IDs; completion IDs must be accept:<requirement_id>. Uncertain delivery requires operator reconciliation, not a new ID.',
                         {'action': {'type': 'string', 'enum': ['send', 'list', 'respond']},
                          'id': {'type': 'string'},
                          'kind': {'type': 'string', 'enum': ['question', 'completion']},
                          'message': {'type': 'string'}, 'answer': {'type': 'string'},
                          'requirement_id': {'type': 'string'}, 'task_id': {'type': 'string'}}, ['action']),
        'ancor_requirement': ('Prepare an immutable draft and exact native clarify_args. Call native clarify separately; only its submitted exact confirmation activates the requirement. cancel and replace accept one or more requirement_ids atomically; respond records the exact human reply as advisory input, never new authorization.',
                              {'action': {'type': 'string', 'enum': ['draft', 'confirm', 'link', 'cancel', 'replace', 'respond']},
                               'body': {'type': 'string'},
                               'brief': {'type': 'string'}, 'acceptance_criteria': {'type': 'string'},
                               'replacement_of': {'type': 'string'}, 'draft_id': {'type': 'string'},
                               'requirement_id': {'type': 'string'}, 'task_id': {'type': 'string'},
                               'requirement_ids': {'type': 'array', 'items': {'type': 'string'}},
                               'reason': {'type': 'string'}}, ['action']),
        'ancor_board': ('Read linked native tasks. Coordinator actions: create, block, unblock, comment, edit title/body/priority, link/unlink parent_id → child_id dependencies. Native ownership and cycle checks apply.',
                        {'action': {'type': 'string', 'enum': ['create', 'list', 'show', 'block', 'unblock', 'comment', 'link', 'unlink', 'edit', 'revise', 'retire', 'associate']},
                         'priority': {'type': 'integer'},
                         'parent_ids': {'type': 'array', 'items': {'type': 'string'}},
                         'hold': {'type': 'boolean'},
                         **{field: {'type': 'string'} for field in
                            ('requirement_id', 'task_id', 'parent_id', 'child_id', 'key', 'title', 'body', 'reason')}}, ['action']),
        'ancor_get_context': ('Read the global requirement snapshot and pending events; acknowledge only explicit event IDs.',
                             {'ack_event_ids': {'type': 'array', 'items': {'type': 'integer'}}}, []),
        'ancor_acceptance': ('Record coordinator verification against exact requirement criteria, or reject with a reason.',
                            {'requirement_id': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['accept', 'reject']},
                             'evidence': {
                                 'type': 'object',
                                 'description': 'Required for accept. Bind exact stored acceptance criteria and independently verified artifacts to linked task IDs.',
                                 'properties': {
                                     'criteria': {'type': 'string', 'description': 'Exact requirement acceptance_criteria text.'},
                                     **{field: {'type': 'array', 'minItems': 1,
                                                'items': {'type': 'string', 'pattern': r'\S'}}
                                        for field in ('verification', 'artifacts', 'task_ids')}},
                                 'required': ['criteria', 'verification', 'artifacts', 'task_ids']},
                             'reason': {'type': 'string'}}, ['requirement_id', 'action']),
    }
    if worker:
        # Workers use native Kanban's task-scoped tools.  The global ANCOR
        # snapshot would expose unrelated requirements and pending events.
        definitions = {}
    elif interactor:
        definitions['ancor_requirement'] = (*definitions['ancor_requirement'][:1],
            {**definitions['ancor_requirement'][1],
             'action': {'type': 'string', 'enum': ['draft', 'confirm', 'respond']}},
            definitions['ancor_requirement'][2])
        definitions['ancor_notify'] = (*definitions['ancor_notify'][:1],
            {**definitions['ancor_notify'][1],
             'action': {'type': 'string', 'enum': ['list', 'respond']}},
            definitions['ancor_notify'][2])
        definitions = {name: value for name, value in definitions.items()
                       if name in {'ancor_requirement', 'ancor_get_context', 'ancor_notify'}}
    elif coordinator:
        definitions['ancor_requirement'] = (*definitions['ancor_requirement'][:1],
            {**definitions['ancor_requirement'][1],
             'action': {'type': 'string', 'enum': ['cancel', 'replace', 'link']}},
            definitions['ancor_requirement'][2])
        definitions['ancor_notify'] = (*definitions['ancor_notify'][:1],
            {**definitions['ancor_notify'][1],
             'action': {'type': 'string', 'enum': ['send', 'list']}},
            definitions['ancor_notify'][2])
    coordination_observed = False
    for name, (description, properties, required) in definitions.items():
        method = methods[name.removeprefix('ancor_')]
        def handle(args, _method=method, _name=name, **kwargs):
            nonlocal coordination_observed
            try:
                if worker:
                    if args:
                        raise PermissionError('worker context is read-only')
                    return json.dumps(service.store.snapshot(), ensure_ascii=False)
                allowed = (interactor_profile if interactor else coordinator_profile)
                if ctx.profile_name != allowed:
                    raise PermissionError('ANCOR role profile required')
                mutation = (_name == 'ancor_acceptance'
                            or (_name == 'ancor_wake' and args.get('action') != 'list')
                            or (_name == 'ancor_notify' and args.get('action') == 'send')
                            or (_name == 'ancor_board' and args.get('action') not in {'list', 'show'})
                            or (_name == 'ancor_requirement' and args.get('action') in {'link', 'cancel', 'replace'})
                            or (_name == 'ancor_get_context' and bool(args.get('ack_event_ids'))))
                # Cooperation marker, not a shell/process security sandbox.
                if mutation and not coordinator:
                    raise PermissionError('coordinator profile required')
                if not coordination_observed and coordinator:
                    from .scheduling import Scheduling
                    Scheduling(service.store).started()
                    coordination_observed = True
                result = _method(args, **kwargs) if _name == 'ancor_notify' else _method(args)
                return json.dumps(result, ensure_ascii=False)
            except Exception as exc:
                log.exception('ANCOR tool failed')
                return json.dumps({'error': str(exc)})
        ctx.register_tool(name=name, toolset='ancor',
                          schema={'name': name, 'description': description, 'parameters': {
                              'type': 'object', 'properties': properties, 'required': required,
                              'additionalProperties': False}}, handler=handle)
