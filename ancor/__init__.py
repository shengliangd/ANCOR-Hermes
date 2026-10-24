"""Thin profile-scoped Hermes integration. No provisioning during registration."""
import asyncio
import hashlib
import json
import logging

from .views import (DRAFT_LIMIT, TRANSITION_LIMIT, acceptance_ref,
                    actionable_notifications, attention_items, awaiting_acceptances,
                    bounded_latest, collection, conditions_with_state, draft_summary,
                    event_counts, event_summaries, host_condition_state,
                    is_open_question, notification_counts, notification_detail,
                    notification_selection, notification_summary, omitted_groups,
                    requirement_counts, requirement_detail, requirement_progress,
                    requirement_ref,
                    select_tasks, task_counts, task_exact_via, transition_summary)

log = logging.getLogger(__name__)

_SCAN_CONNECTION_POLL_SECONDS = 1.0


_ACTION_FIELDS = {
    'ancor_requirement': {
        'draft': ['title', 'brief', 'acceptance_criteria'],
        'confirm': ['draft_id'],
        'cancel': ['requirement_ids'],
        'pause': ['requirement_id', 'reason'],
        'resume': ['requirement_id', 'reason'],
        'advance': ['requirement_id'],
        'respond': ['requirement_id', 'body'],
        'accept': ['requirement_id'],
        'request_changes': ['requirement_id', 'feedback'],
    },
    'ancor_notify': {
        'send': ['id', 'kind', 'message'],
        'withdraw': ['id', 'reason'],
        'list': [],
        'respond': ['id', 'answer'],
    },
    'ancor_board': {
        'create': ['requirement_id', 'key', 'title', 'contract'],
        'list': [],
        'show': ['task_id'],
        'history': ['task_id', 'kind'],
        'block': ['task_id', 'reason'],
        'unblock': ['task_id', 'reason'],
        'comment': ['task_id', 'body'],
        'link': ['parent_id', 'child_id'],
        'unlink': ['parent_id', 'child_id'],
        'edit': ['task_id'],
        'revise': ['task_id', 'reason'],
        'archive': ['task_id', 'reason'],
        'associate': ['task_id', 'requirement_id', 'reason'],
    },
    'ancor_wake': {
        'schedule': ['requirement_id', 'reason', 'due_at'],
        'list': [],
        'cancel': ['wake_id'],
        'replace': ['wake_id', 'reason', 'due_at'],
    },
    'ancor_acceptance': {
        'accept': ['requirement_id', 'evidence'],
        'reject': ['requirement_id', 'reason'],
    },
    'ancor_note': {
        'list': ['requirement_id'],
        'get': ['requirement_id', 'note_id'],
        'add': ['requirement_id', 'body'],
        'update': ['requirement_id', 'note_id', 'body'],
        'delete': ['requirement_id', 'note_id'],
    },
}


def _missing_fields(args, fields):
    missing = []
    for field in fields:
        value = args.get(field)
        if (field not in args or value is None
                or (isinstance(value, str) and not value.strip())
                or (isinstance(value, (list, dict)) and not value)):
            missing.append(field)
    return missing


def _invalid_request(name, action, *, missing=(), reason=None, next_step=None,
                     valid_actions=None, required_any=None, valid_kinds=None):
    missing = list(missing)
    result = {
        'error': reason or f'{name} {action} is missing required fields: {", ".join(missing)}',
        'code': 'invalid_request',
        'tool': name,
        'action': action,
    }
    if missing:
        result['missing_fields'] = missing
    if required_any:
        result['required_any'] = list(required_any)
    if valid_actions:
        result['valid_actions'] = list(valid_actions)
    if valid_kinds:
        result['valid_kinds'] = list(valid_kinds)
    result['next_step'] = next_step or (
        f'Provide {", ".join(missing)} and retry {name} with action={action}.')
    return result


def _validate_action_request(name, args):
    """Return a model-actionable error before any business method runs."""
    fields = _ACTION_FIELDS.get(name)
    if fields is None:
        return None
    action = args.get('action')
    if not isinstance(action, str) or not action.strip():
        return _invalid_request(
            name, action, missing=['action'],
            reason=f'{name} requires action',
            next_step=f'Choose one of: {", ".join(sorted(fields))}.',
            valid_actions=sorted(fields))
    if action not in fields:
        return _invalid_request(
            name, action,
            reason=f'{name} does not support action {action!r}',
            next_step=f'Choose one of: {", ".join(sorted(fields))}.',
            valid_actions=sorted(fields))
    missing = _missing_fields(args, fields[action])
    if missing:
        return _invalid_request(name, action, missing=missing)
    if name == 'ancor_board' and action == 'edit':
        editable = ['title', 'body', 'priority']
        if not any(field in args and args[field] is not None
                   and not (isinstance(args[field], str) and not args[field].strip())
                   for field in editable):
            return _invalid_request(
                name, action,
                reason='ancor_board edit requires at least one of title, body or priority',
                next_step='Provide title, body or priority, then retry ancor_board edit.',
                required_any=editable)
    if name == 'ancor_notify' and action == 'send':
        kind = args.get('kind')
        if kind == 'question':
            missing = _missing_fields(args, ['requirement_id', 'task_id'])
            if missing:
                return _invalid_request(name, action, missing=missing)
        elif kind == 'completion':
            missing = _missing_fields(args, ['requirement_id'])
            if missing:
                return _invalid_request(name, action, missing=missing)
        elif kind not in {'question', 'completion'}:
            return _invalid_request(
                name, action,
                reason=f'ancor_notify send does not support kind {kind!r}',
                next_step='Choose kind=question or kind=completion.',
                valid_kinds=['question', 'completion'])
    return None


def register(ctx, *, scan_runner=None):
    if scan_runner is None:
        from .host import scan_runner
    if ctx.profile_name == ctx.get_config('owner_profile', 'default') and ctx.get_config('gateway_owner', False):
        def connected(native, adapter):
            # Hermes owns plugin registrations per discovery generation: force re-discovery
            # disposes them (including supervised tasks), while same-key plugins are not
            # re-wired. A Gateway-lifetime task attached to the long-lived adapter keeps the
            # durable inbox flowing across plugin updates. A Gateway restart still owns the
            # final lifecycle and reloads the scanner code.
            current = getattr(adapter, '_ancor_scan_task', None)
            if current is not None and not current.done():
                return
            loop = asyncio.get_running_loop()

            async def supervise():
                while not adapter.is_connected:
                    await asyncio.sleep(_SCAN_CONNECTION_POLL_SECONDS)
                while adapter.is_connected:
                    try:
                        await scan_runner(ctx, adapter)
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception('ANCOR scan supervisor failed; retrying')
                        if not adapter.is_connected:
                            return
                        await asyncio.sleep(15)

            task = loop.create_task(supervise(), name='ancor:scan')
            setattr(adapter, '_ancor_scan_task', task)
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
        from .notification import NotificationOutbox
        # Fail closed: destination is explicit. Delivery is Gateway-owned.
        return NotificationOutbox(
            service.store.path + '.notifications.db',
            coordinator_profile=coordinator_profile,
            target=ctx.get_config('human_target'))

    def response_event(row):
        try:
            reference = json.loads(row.get('answer_reference') or '{}')
        except (TypeError, ValueError):
            reference = {}
        meta = reference if isinstance(reference, dict) else {}
        confirmed = meta.get('source') == 'native_clarify_response'
        return service.store.add_event('notification.response', {
            'notification_id': row['id'], 'body': row['answer'],
            'requirement_id': row.get('requirement_id'), 'task_id': row.get('task_id'),
            'reference': row['answer_reference'],
            'source': ('native_clarify_response' if confirmed
                       else 'interactor_transcription'),
            'confirmed': confirmed,
            'authorization': False},
            source_key='notification.response:' + row['id'])

    def notifications():
        rows = outbox().list()
        # Idempotent input ingestion, not a coordination decision.
        for row in rows:
            if row['answer'] is not None:
                response_event(row)
        return rows

    def wake(args, **_native):
        from .scheduling import Scheduling
        return Scheduling(service.store).call(args)

    def validate_acks(event_ids, snapshot, tasks):
        pending = {event['id']: event for event in snapshot['pending_events']}
        task_by_id = {task['id']: task for task in tasks or ()}
        for identifier in event_ids:
            event = pending.get(identifier)
            if event is None:
                continue
            if event['kind'] in {'host.stalled', 'host.liveness'}:
                raise ValueError(f'event {identifier} is host-owned and clears only when the requirement recovers')
            payload = event.get('payload') or {}
            task_id = payload.get('task_id')
            task = task_by_id.get(task_id, {})
            if event['kind'] == 'native.block_loop_detected' and task.get('status') == 'triage':
                raise ValueError(f'event {identifier} cannot be acknowledged while {task_id} is in triage')

    def ack_rejections(event_ids, retained, tasks):
        """Validate an entire ack batch without writing any acknowledgement."""
        by_id = {event['id']: event for event in retained}
        task_by_id = {task['id']: task for task in tasks or ()}
        rejected = []
        for identifier in event_ids:
            event = by_id.get(identifier)
            if event is None:
                rejected.append({'id': identifier, 'reason': 'not_found',
                                 'message': f'event {identifier} does not exist'})
                continue
            if event['kind'] in {'host.stalled', 'host.liveness'}:
                rejected.append({
                    'id': identifier, 'reason': 'host_owned',
                    'message': f'event {identifier} is host-owned and clears only through host state'})
                continue
            payload = event.get('payload') or {}
            task_id = payload.get('task_id')
            task = task_by_id.get(task_id, {})
            if event['kind'] == 'native.block_loop_detected' and task.get('status') == 'triage':
                rejected.append({
                    'id': identifier, 'reason': 'triage_block_loop',
                    'message': f'event {identifier} cannot be acknowledged while {task_id} is in triage'})
        return rejected

    def ack_events(args, **_native):
        event_ids = args.get('event_ids')
        if (not isinstance(event_ids, list) or not event_ids
                or any(type(identifier) is not int or identifier < 1
                       for identifier in event_ids)):
            raise ValueError('event_ids must contain positive integer IDs')
        retained = service.store.events_by_ids(event_ids)
        tasks = operations.tasks(include_archived=True)
        rejected = ack_rejections(event_ids, retained, tasks)
        if rejected:
            return {'acked': [], 'already_acked': [], 'rejected': rejected,
                    'remaining_pending': len(service.store.pending_events())}
        by_id = {event['id']: event for event in retained}
        acked, already = [], []
        for identifier in dict.fromkeys(event_ids):
            if by_id[identifier]['acknowledged']:
                already.append(identifier)
            else:
                acked.append(identifier)
        if acked:
            service.store.ack_events(acked)
        reason = args.get('reason')
        if reason is not None:
            log.info('ANCOR acknowledged %d event(s): %s', len(acked), reason)
        result = {'acked': acked, 'already_acked': already, 'rejected': [],
                  'remaining_pending': len(service.store.pending_events())}
        if reason is not None:
            result['reason'] = reason
        return result

    def get_context(args, **_native):
        requirement_id = args.get('requirement_id')
        task_limit = args.get('task_limit', 200 if requirement_id else 50)
        if type(task_limit) is not int or not 1 <= task_limit <= 200:
            raise ValueError('task_limit must be an integer between 1 and 200')
        notification_limit = args.get('notification_limit', 10)
        if type(notification_limit) is not int or not 1 <= notification_limit <= 200:
            raise ValueError('notification_limit must be an integer between 1 and 200')
        attention_limit = args.get('attention_limit', 8)
        if type(attention_limit) is not int or not 0 <= attention_limit <= 50:
            raise ValueError('attention_limit must be an integer between 0 and 50')
        event_limit = args.get('event_limit', 100)
        if type(event_limit) is not int or not 1 <= event_limit <= 500:
            raise ValueError('event_limit must be an integer between 1 and 500')
        note_limit = args.get('note_limit', 20)
        if coordinator and (type(note_limit) is not int or not 1 <= note_limit <= 200):
            raise ValueError('note_limit must be an integer between 1 and 200')
        event_id = args.get('event_id')
        if event_id is not None and (type(event_id) is not int or event_id < 1):
            raise ValueError('event_id must be a positive integer')
        include_done = args.get('include_done', False)
        include_archived = args.get('include_archived', False)
        include_closed_notifications = args.get('include_closed_notifications', False)
        progress_only = args.get('progress_only', False)
        for name, value in (
                ('include_done', include_done),
                ('include_archived', include_archived),
                ('include_closed_notifications', include_closed_notifications),
                ('progress_only', progress_only)):
            if type(value) is not bool:
                raise ValueError(f'{name} must be a boolean')
        if progress_only and requirement_id:
            raise ValueError('progress_only cannot be combined with requirement_id')
        if event_id is not None:
            if not coordinator:
                raise PermissionError('coordinator profile required to inspect events')
            retained = service.store.events_by_ids([event_id])
            if not retained:
                raise KeyError(event_id)
            return {'event': retained[0],
                    'expand': 'Use ancor_board show with the payload task_id for full task evidence.'}
        try:
            notification_rows = notifications()
        except ValueError as exc:
            notification_rows = []
            notification_error = str(exc)
        else:
            notification_error = None
        extra = {'notifications_error': notification_error} if notification_error else {}
        all_tasks = None
        try:
            all_tasks = operations.tasks(include_archived=True)
        except Exception as exc:
            extra['board_error'] = str(exc)
        if args.get('ack_event_ids'):
            validate_acks(args['ack_event_ids'], service.store.snapshot(), all_tasks or [])
        data = service.get_context(args if coordinator else {})
        from .scheduling import Scheduling
        observation = Scheduling(service.store).observation(
            data, all_tasks, notification_rows)
        drafts_by_id = {draft['id']: draft for draft in data['drafts']}
        progression = {item['requirement_id']: item
                       for item in observation['progression']}
        expand = ('Call ancor_get_context with requirement_id=<id> for exact requirement text'
                  + (', event_id=<id> for one complete event, or ancor_ack_events for handled IDs' if coordinator else '')
                  + '; call ancor_board show with task_id=<id> for the full contract, '
                    'comments, runs and events.')
        deprecated = ({'deprecated': 'use ancor_ack_events'}
                      if args.get('ack_event_ids') else {})
        ordinary_events = [event for event in data['pending_events']
                           if event['kind'] != 'host.stalled']
        conditions = conditions_with_state(
            observation['progression'], host_condition_state(service.store.path))
        if requirement_id:
            requirement = service.store.get_requirement(requirement_id)
            linked = [task for task in all_tasks or []
                      if task.get('requirement_id') == requirement_id]
            selected_tasks = select_tasks(
                linked, include_done=args.get('include_done', True),
                include_archived=args.get('include_archived', True))
            visible = selected_tasks[:task_limit]
            relevant = [row for row in notification_rows
                        if row.get('requirement_id') == requirement_id]
            task_ids = {task['id'] for task in linked}
            events = [event for event in ordinary_events
                      if (event.get('payload') or {}).get('requirement_id') == requirement_id
                      or (event.get('payload') or {}).get('task_id') in task_ids]
            filtered = {**data, 'requirements': [requirement],
                        'task_links': [link for link in data['task_links']
                                       if link['requirement_id'] == requirement_id]}
            scoped = Scheduling(service.store).observation(
                filtered, linked, notification_rows)
            acceptance_events = [event for event in service.store.acceptances()
                                 if (event.get('payload') or {}).get('requirement_id') == requirement_id]
            selected_notifications = notification_selection(relevant, notification_limit)
            result = {
                'requirement': requirement_detail(
                    requirement, drafts_by_id.get(requirement['draft_id'])),
                'tasks': collection(
                    [{**task, 'exact_via': task_exact_via(task['id'])} for task in visible],
                    total=len(selected_tasks)),
                'counts': {'tasks': task_counts(linked),
                           'notifications': notification_counts(relevant)},
                'acceptance': acceptance_ref(
                    requirement, linked,
                    acceptance_events[-1] if acceptance_events else None),
                'archived_task_count': sum(
                    1 for task in linked
                    if task.get('archived_at') or task.get('archived')),
                'notifications': collection(
                    [notification_summary(row, exact_via=True)
                     for row in selected_notifications],
                    total=len(relevant)),
                'omitted': omitted_groups(linked, relevant),
                'open_question_count': sum(
                    is_open_question(row) for row in relevant),
                'transitions': bounded_latest(
                    [transition_summary(event) for event in
                     service.store.requirement_transitions(requirement_id)],
                    TRANSITION_LIMIT),
                'conditions': [condition for condition in conditions
                               if condition['requirement_id'] == requirement_id],
                'progression': scoped['progression'],
                'liveness': scoped['liveness'],
                'scheduled_wakes': scoped['scheduled_wakes'],
                'expand': expand,
                **extra,
                **deprecated,
            }
            if coordinator:
                result['pending_events'] = collection(
                    event_summaries(events, event_limit, exact_via=True), total=len(events))
                result['notes'] = service.note({
                    'action': 'list', 'requirement_id': requirement_id,
                    'limit': note_limit})
            else:
                result['pending_events'] = collection([], total=len(events))
            return result
        confirmed_drafts = {requirement['draft_id'] for requirement in data['requirements']}
        open_requirements = [requirement for requirement in data['requirements']
                             if (requirement['status'] in {'active', 'paused', 'pending'}
                                 or requirement.get('target_status') is not None)]
        if progress_only:
            tasks_by_requirement = {}
            for task in all_tasks or ():
                tasks_by_requirement.setdefault(
                    task.get('requirement_id'), []).append(task)
            reviews = awaiting_acceptances(data['requirements'], notification_rows)
            rows = [requirement_progress(
                requirement,
                progression.get(requirement['id']),
                tasks_by_requirement.get(requirement['id'], ()),
                notification_rows)
                for requirement in open_requirements]
            return {
                'requirements': collection(rows),
                'awaiting_acceptance': collection(reviews),
                'counts': {
                    'open_requirements': len(rows) + len(reviews),
                    'awaiting_acceptance': len(reviews),
                    'open_questions': sum(
                        is_open_question(row) for row in notification_rows),
                },
                'expand': ('Call ancor_get_context with requirement_id=<id> for '
                           'exact requirement text or ancor_board show for one task.'),
                **extra,
            }
        selected_tasks = select_tasks(
            all_tasks or [], include_done=include_done,
            include_archived=include_archived)
        visible = selected_tasks[:task_limit]
        notification_source = (notification_rows if include_closed_notifications
                               else actionable_notifications(notification_rows))
        selected_notifications = notification_selection(
            notification_source, notification_limit)
        reviews = awaiting_acceptances(data['requirements'], notification_rows)
        result = {
            'requirements': collection(
                [requirement_ref(requirement, progression.get(requirement['id']),
                                 exact_via=True)
                 for requirement in open_requirements]),
            'tasks': collection(
                [{**task, 'exact_via': task_exact_via(task['id'])} for task in visible],
                total=len(selected_tasks)),
            'counts': {
                'requirements': requirement_counts(data['requirements']),
                'tasks': task_counts(all_tasks or []),
                'events': event_counts(ordinary_events),
                'notifications': notification_counts(notification_rows),
                'awaiting_acceptance': len(reviews),
            },
            'awaiting_acceptance': collection(reviews),
            'attention': attention_items(
                observation['progression'], tasks=all_tasks,
                notifications=actionable_notifications(notification_rows),
                events=ordinary_events, limit=attention_limit),
            'omitted': omitted_groups(all_tasks or [], notification_rows),
            'conditions': conditions,
            'notifications': collection(
                [notification_summary(row, exact_via=True)
                 for row in selected_notifications],
                total=len(notification_source)),
            'open_question_count': sum(
                is_open_question(row) for row in notification_rows),
            'progression': observation['progression'],
            'liveness': observation['liveness'],
            'scheduled_wakes': observation['scheduled_wakes'],
            'expand': expand,
            **extra,
            **deprecated,
        }
        if interactor:
            drafts = [draft_summary(draft) for draft in data['drafts']
                      if draft['id'] not in confirmed_drafts]
            result['drafts'] = collection(
                drafts[:DRAFT_LIMIT], total=len(drafts))
        if coordinator:
            result['pending_events'] = collection(
                event_summaries(ordinary_events, event_limit, exact_via=True),
                total=len(ordinary_events))
        else:
            result['pending_events'] = collection([], total=len(ordinary_events))
        return result

    def notify(args, **native):
        action = args['action']
        if action == 'list':
            rows = notifications()
            identifier = args.get('id')
            if identifier:
                rows = [row for row in rows if row['id'] == identifier]
                if not rows:
                    raise KeyError(identifier)
            state = args.get('state')
            if state:
                rows = [row for row in rows if row['state'] == state]
            for field in ('requirement_id', 'task_id'):
                value = args.get(field)
                if value:
                    rows = [row for row in rows if row.get(field) == value]
            if args.get('open_only'):
                rows = [row for row in rows if row['kind'] == 'question'
                        and row['answer'] is None and row.get('closed_at') is None]
            limit = args.get('limit', 50)
            if type(limit) is not int or not 1 <= limit <= 200:
                raise ValueError('limit must be an integer between 1 and 200')
            total = len(rows)
            rows = rows[-limit:]
            full = bool(identifier)
            items = ([notification_detail(row) for row in rows] if full else
                     [notification_summary(row) for row in rows])
            return {'notifications': collection(items, total=total)}
        box = outbox()
        identifier = args['id']
        if action == 'send':
            if not coordinator:
                raise PermissionError('coordinator profile required to send notifications')
            requirement_id, task_id = args.get('requirement_id'), args.get('task_id')
            try:
                existing = box.get(identifier)
            except KeyError:
                existing = None
            retry_same_question = bool(
                existing and existing['kind'] == 'question'
                and existing['requirement_id'] == requirement_id
                and existing['task_id'] == task_id)
            if args['kind'] == 'completion':
                req = service.store.get_requirement(requirement_id)
                if req['status'] != 'awaiting_acceptance':
                    raise ValueError('completion requires a requirement awaiting human acceptance')
                if identifier != 'accept:' + req['id']:
                    raise ValueError('completion id must be accept:<requirement_id>')
            if args['kind'] == 'question':
                if not task_id:
                    raise ValueError('question requires requirement_id and task_id')
                req = service.store.get_requirement(requirement_id)
                if req['status'] != 'active' or req['target_status'] is not None:
                    raise ValueError('question requires a stably active requirement')
                links = {link['task_id'] for link in service.store.snapshot()['task_links']
                         if link['requirement_id'] == req['id']}
                if task_id not in links:
                    raise ValueError('question task must be linked to the requirement')
                if not retry_same_question:
                    task = operations.board.show(task_id)
                    if task.get('archived_at') or task.get('archived'):
                        raise RuntimeError('question requires an unarchived task')
                    if task.get('status') != 'blocked':
                        if task.get('status') == 'triage':
                            raise RuntimeError(
                                f'question requires a blocked task; {task_id} is in triage, '
                                'which must be reviewed and revised before asking the human')
                        raise RuntimeError(
                            f'question requires a blocked task; {task_id} is '
                            f'{task.get("status")}')
            box.enqueue(identifier, args['kind'], args['message'], actor=ctx.profile_name,
                        requirement_id=requirement_id, task_id=task_id)
            return notification_detail(box.get(identifier))
        if action == 'withdraw':
            if not coordinator:
                raise PermissionError('coordinator profile required to withdraw questions')
            return notification_detail(box.withdraw(identifier, actor=ctx.profile_name,
                                                    reason=args['reason']))
        if action == 'respond':
            if not interactor:
                raise PermissionError('interactor profile required to respond')
            existing = box.get(identifier)
            if existing['kind'] != 'question':
                raise ValueError('not a question')
            if existing['answer'] is not None:
                if existing['answer'] != args['answer']:
                    raise ValueError('question already answered')
                event = response_event(existing)
                return {'notification': notification_detail(existing),
                        'event': event, 'confirmed': event['payload']['confirmed']}
            # The reply is not durable here. The returned proposal is immutable
            # and only its exact native confirmation commits the answer.
            proposal = box.propose_response(identifier, args['answer'])
            return {'notification': notification_detail(existing),
                    **service.response_presentation(proposal)}
        raise ValueError('unknown notification action')

    def requirement(args, **_native):
        if args['action'] != 'respond':
            return service.requirement(args)
        req = service.store.get_requirement(args['requirement_id'])
        body = service.store._text(args['body'])
        # Foreground transcription is advisory input, never consent evidence.
        return service.store.add_event('requirement.response', {
            'requirement_id': req['id'], 'body': body,
            'source': 'interactor_transcription', 'authorization': False},
            source_key='requirement.response:' + req['id'] + ':' +
            hashlib.sha256(body.encode('utf-8')).hexdigest())

    def board_action(args, **_native):
        return operations.call(args)

    def acceptance(args, **_native):
        if args['action'] == 'accept':
            operations.reconcile()
        return service.acceptance(args)

    def note(args, **_native):
        return service.note(args)

    methods = {'requirement': requirement, 'get_context': get_context,
               'ack_events': ack_events, 'notify': notify,
               'acceptance': acceptance, 'board': board_action, 'wake': wake,
               'note': note}
    if interactor:
        ctx.register_hook('pre_tool_call', service.pre_tool_call)
        ctx.register_hook('post_tool_call', service.post_tool_call)
    contract_schema = {
        'type': 'object',
        'description': ('Worker card. It holds these six parts and nothing else. '
                        'outcome, expected_output and evidence are required; inputs, '
                        'advice and binding_constraints may be empty.'),
        'properties': {
            'outcome': {'type': 'string', 'pattern': r'\S'},
            'inputs': {'type': 'array', 'items': {'type': 'string', 'pattern': r'\S'}},
            'advice': {'type': 'array', 'items': {'type': 'string', 'pattern': r'\S'}},
            'binding_constraints': {'type': 'array',
                                    'items': {'type': 'string', 'pattern': r'\S'}},
            'expected_output': {'type': 'array', 'minItems': 1,
                                'items': {'type': 'string', 'pattern': r'\S'}},
            'evidence': {'type': 'array', 'minItems': 1,
                         'items': {'type': 'string', 'pattern': r'\S'}},
        },
        'required': ['outcome', 'expected_output', 'evidence'],
        'additionalProperties': False,
    }
    if worker:
        # Workers use native Kanban's task-scoped tools and the task card contract.
        from .worker_context import transform_kanban_show_result
        ctx.register_hook('transform_tool_result', transform_kanban_show_result)
        definitions = {}
    elif interactor:
        definitions = {
            'ancor_requirement': (
                'Draft, confirm, cancel, pause, resume, answer, or make the final human acceptance decision on an ANCOR requirement. draft requires an authoritative single-line title plus an outcome-oriented brief and observable acceptance criteria; keep implementation method and task decomposition out unless the user made a detail a hard constraint. cancel, pause, resume, accept and request_changes create immutable proposals with exact native clarify_args. accept is final human acceptance of coordinator-verified work; request_changes requires exact feedback and returns the requirement to active repair. respond records a human reply as advisory input, never authorization.',
                {'action': {'type': 'string', 'enum': ['draft', 'confirm', 'cancel', 'pause', 'resume', 'respond', 'accept', 'request_changes']},
                 'title': {'type': 'string', 'maxLength': 80,
                           'description': 'Authoritative single-line requirement label used by every compact list and wake packet.'},
                 'summary': {'type': 'string',
                             'description': 'Optional high-level, user-facing points shown before the exact stored requirement; may state which execution choices are left open.'},
                 'brief': {'type': 'string',
                           'description': 'Outcome, scope, hard constraints, non-goals and unresolved human decisions. Do not prescribe implementation unless externally fixed or explicitly non-negotiable.'},
                 'acceptance_criteria': {'type': 'string',
                                         'description': 'Observable end states and important failure boundaries; not test steps, evidence formats or internal implementation details.'},
                 'replacement_of': {'oneOf': [{'type': 'string'},
                                               {'type': 'array', 'items': {'type': 'string'}}]},
                 'draft_id': {'type': 'string'}, 'requirement_id': {'type': 'string'},
                 'requirement_ids': {'type': 'array', 'items': {'type': 'string'}},
                 'reason': {'type': 'string'}, 'body': {'type': 'string'},
                 'feedback': {'type': 'string',
                              'description': 'Exact human change request. Required only for request_changes.'}}, ['action']),
            'ancor_get_context': (
                'Read a compact ANCOR overview: open draft previews, requirement title references, open task references, counts, attention items, open questions and health. Set progress_only=true for a minimal open-work view; it retains coordinator-verified requirements awaiting final human acceptance, while excluding history. Accepted requirements are closed; awaiting_acceptance requirements are still open work. Terminal tasks and closed notifications are summarized in counts/omitted instead of listed. Pass requirement_id to expand one requirement with its exact brief and acceptance criteria. Read-only: this role cannot acknowledge events or mutate the board.',
                {'requirement_id': {'type': 'string'},
                 'task_limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                 'notification_limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                 'attention_limit': {'type': 'integer', 'minimum': 0, 'maximum': 50},
                 'include_done': {'type': 'boolean'},
                 'include_archived': {'type': 'boolean'},
                 'include_closed_notifications': {'type': 'boolean'},
                 'progress_only': {'type': 'boolean',
                                   'description': 'Return only open requirements as compact progress rows.'}}, []),
            'ancor_board': (
                'Read linked native tasks. list returns compact summaries and accepts requirement_id, status, limit and include_archived filters; show returns one card with bounded recent history; history expands comments, events or runs. This role cannot mutate the board.',
                {'action': {'type': 'string', 'enum': ['list', 'show', 'history']},
                 'kind': {'type': 'string', 'enum': ['comments', 'events', 'runs']},
                 'before_id': {'type': 'integer', 'minimum': 1},
                 'requirement_id': {'type': 'string'}, 'task_id': {'type': 'string'},
                 'status': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                 'include_archived': {'type': 'boolean'}}, ['action']),
            'ancor_notify': (
                'List compact durable notifications, expand one by id, or answer an open question. respond returns an immutable response proposal with exact native clarify_args; the reply is not recorded until the user submits that exact confirmation. Completion notices are closed only by the final human acceptance decision in ancor_requirement. Filter by requirement_id, task_id, state or open_only. A closed question cannot be resurrected; use requirement respond for new human input outside a question.',
                {'action': {'type': 'string', 'enum': ['list', 'respond']},
                 'id': {'type': 'string'}, 'answer': {'type': 'string'},
                 'state': {'type': 'string'}, 'open_only': {'type': 'boolean'},
                 'requirement_id': {'type': 'string'}, 'task_id': {'type': 'string'},
                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['action']),
        }
    elif coordinator:
        definitions = {
            'ancor_requirement': (
                'Advance one requested requirement transition after its linked native cards have converged. advance is idempotent: it returns completed, or blocked with concrete card or replacement blockers. It is the only coordinator path that commits a staged pause, resume, cancellation or replacement.',
                {'action': {'type': 'string', 'enum': ['advance']},
                 'requirement_id': {'type': 'string'}}, ['action', 'requirement_id']),
            'ancor_note': (
                'Maintain requirement-scoped working notes across coordinator wakes. Notes are durable coordination memory, not requirement text, authorization, acceptance evidence, or a task contract. list returns bounded previews; get returns one full note. Every note carries created_at and updated_at Unix timestamps. add, update and delete require a stable active or paused requirement; delete is a soft delete and revisions are retained. Never store credentials or secrets.',
                {'action': {'type': 'string', 'enum': ['list', 'get', 'add', 'update', 'delete']},
                 'requirement_id': {'type': 'string'}, 'note_id': {'type': 'string'},
                 'body': {'type': 'string'},
                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                 'include_deleted': {'type': 'boolean'}},
                ['action', 'requirement_id']),
            'ancor_wake': (
                'Manage durable future one-shot wakes. schedule requires requirement_id, reason, due_at (Unix seconds); replace requires wake_id, reason, due_at; cancel requires wake_id. list defaults to pending wakes and optionally filters requirement_id or state. Never create immediate self-wake loops.',
                {'action': {'type': 'string', 'enum': ['schedule', 'list', 'cancel', 'replace']},
                 'requirement_id': {'type': 'string'}, 'wake_id': {'type': 'string'},
                 'reason': {'type': 'string'}, 'due_at': {'type': 'number'},
                 'state': {'type': 'string', 'enum': ['scheduled', 'queued', 'suppressed',
                                                      'cancelled', 'handled']},
                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['action']),
            'ancor_notify': (
                'Durably enqueue a neutral question or accepted completion, list compact notifications, or withdraw an unanswered question that later information made obsolete. Enqueue returns pending; Gateway delivery is asynchronous. A question requires an active requirement and a linked blocked task; multiple distinct questions may remain open for the same requirement or task. Completion requires an accepted requirement and id accept:<requirement_id>. Filter by requirement_id, task_id, state or open_only; only id expands one full notification. Reuse a stable ID for the same question; pending is not delivery, and uncertain requires operator reconciliation.',
                {'action': {'type': 'string', 'enum': ['send', 'list', 'withdraw']},
                 'id': {'type': 'string'},
                 'kind': {'type': 'string', 'enum': ['question', 'completion']},
                 'message': {'type': 'string'}, 'reason': {'type': 'string'},
                 'requirement_id': {'type': 'string'}, 'task_id': {'type': 'string'},
                 'state': {'type': 'string'}, 'open_only': {'type': 'boolean'},
                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['action']),
            'ancor_board': (
                'Read and coordinate linked native tasks. list returns compact summaries; show returns one card with bounded recent history; history expands comments, events or runs for one card. create requires a contract with outcome, expected_output and evidence; inputs, advice and binding_constraints are optional; edit takes the same contract and re-renders the card. archive writes an audit comment and verifies the native terminal state. Supports create, block, unblock, comment, link/unlink parent_id -> child_id, revise, archive, and associate.',
                {'action': {'type': 'string', 'enum': ['create', 'list', 'show', 'block', 'unblock',
                                                        'history', 'comment', 'link', 'unlink', 'edit', 'revise',
                                                        'archive', 'associate']},
                 'kind': {'type': 'string', 'enum': ['comments', 'events', 'runs']},
                 'before_id': {'type': 'integer', 'minimum': 1},
                 'priority': {'type': 'integer'},
                 'parent_ids': {'type': 'array', 'items': {'type': 'string'}},
                 'hold': {'type': 'boolean'},
                 'status': {'type': 'string'},
                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                 'include_archived': {'type': 'boolean'},
                 'contract': contract_schema,
                 **{field: {'type': 'string'} for field in
                    ('requirement_id', 'task_id', 'parent_id', 'child_id', 'key', 'title', 'reason')}},
                ['action']),
            'ancor_get_context': (
                'Read a compact coordination snapshot: counts, attention items, open task references, open questions, host conditions, pending event previews and health for active or paused requirements. Terminal tasks and closed notifications are summarized in counts/omitted instead of listed; set include_done, include_archived or include_closed_notifications only when history is needed. Payloads are previews; pass requirement_id for exact requirement text, ancor_board show for a full task, and ancor_notify id for one full notification. Pass event_id to read one complete event, including one already acknowledged. Use ancor_ack_events for handled IDs; ack_event_ids is deprecated.',
                {'ack_event_ids': {'type': 'array', 'items': {'type': 'integer'}},
                 'requirement_id': {'type': 'string'},
                 'notification_limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                 'attention_limit': {'type': 'integer', 'minimum': 0, 'maximum': 50},
                 'include_done': {'type': 'boolean'},
                 'include_archived': {'type': 'boolean'},
                 'include_closed_notifications': {'type': 'boolean'},
                 'task_limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                 'event_limit': {'type': 'integer', 'minimum': 1, 'maximum': 500},
                 'event_id': {'type': 'integer', 'minimum': 1},
                 'note_limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, []),
            'ancor_ack_events': (
                'Atomically acknowledge durable event IDs after acting on them. The whole batch is rejected without writing if any ID is missing, host-owned (host.stalled or host.liveness), or blocked by a triage card. Repeating an acknowledged ID is idempotent and returns it in already_acked.',
                {'event_ids': {'type': 'array', 'minItems': 1,
                               'items': {'type': 'integer', 'minimum': 1}},
                 'reason': {'type': 'string'}}, ['event_ids']),
            'ancor_acceptance': (
                'Record independent coordinator verification against exact requirement criteria, or reject with a reason. Verification requires every linked native card to be done or archived and archives verified done cards. verify/accept moves the requirement to awaiting_acceptance; it is not final acceptance. The human-facing role makes the final decision.',
                {'requirement_id': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['verify', 'accept', 'reject']},
                 'evidence': {
                     'type': 'object',
                     'description': 'Required for verify/accept. Bind exact stored acceptance criteria and independently verified artifacts to linked task IDs.',
                     'properties': {
                         'criteria': {'type': 'string', 'description': 'Exact requirement acceptance_criteria text.'},
                         **{field: {'type': 'array', 'minItems': 1,
                                    'items': {'type': 'string', 'pattern': r'\S'}}
                            for field in ('verification', 'artifacts', 'task_ids')}},
                     'required': ['criteria', 'verification', 'artifacts', 'task_ids']},
                 'reason': {'type': 'string'}}, ['requirement_id', 'action']),
        }

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
                if _name == 'ancor_requirement' and coordinator and args.get('action') != 'advance':
                    raise PermissionError('coordinator may only advance requirements')
                mutation = (_name == 'ancor_acceptance'
                            or _name == 'ancor_ack_events'
                            or (_name == 'ancor_requirement' and args.get('action') == 'advance')
                            or (_name == 'ancor_wake' and args.get('action') != 'list')
                            or (_name == 'ancor_note' and args.get('action') in {'add', 'update', 'delete'})
                            or (_name == 'ancor_notify' and args.get('action') == 'send')
                            or (_name == 'ancor_board' and args.get('action') not in {'list', 'show'})
                            or (_name == 'ancor_get_context' and bool(args.get('ack_event_ids'))))
                # Cooperation marker, not a shell/process security sandbox.
                if mutation and not coordinator:
                    raise PermissionError('coordinator profile required')
                invalid = _validate_action_request(_name, args)
                if invalid:
                    return json.dumps(invalid, ensure_ascii=False)
                if not coordination_observed and coordinator:
                    from .scheduling import Scheduling
                    Scheduling(service.store).started()
                    coordination_observed = True
                result = _method(args, **kwargs)
                return json.dumps(result, ensure_ascii=False)
            except Exception as exc:
                log.exception('ANCOR tool failed')
                code = ('forbidden' if isinstance(exc, PermissionError) else
                        'not_found' if isinstance(exc, KeyError) else
                        'invalid_request' if isinstance(exc, ValueError) else
                        'state_conflict' if isinstance(exc, RuntimeError) else 'internal')
                return json.dumps({'error': str(exc), 'code': code}, ensure_ascii=False)
        ctx.register_tool(name=name, toolset='ancor',
                          schema={'name': name, 'description': description, 'parameters': {
                              'type': 'object', 'properties': properties, 'required': required,
                              'additionalProperties': False}}, handler=handle)
