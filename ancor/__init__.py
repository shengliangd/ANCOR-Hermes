"""Thin 2-profile Hermes integration for ANCOR (Plan A: default + ancor-exec).

- ``default`` profile: user's everyday Interactor (plus Gateway owner when
  ``gateway_owner=True``).
- ``ancor-exec`` profile: single headless execution sandbox; exact role is
  selected per process by ``ANCOR_ROLE=root|goal|judge`` (plus ``GOAL_ID`` for
  ``goal`` nodes), with a ``pre_tool_call`` fence enforcing tool isolation.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path

from .atomic_tools import interfaces as atomic_interfaces, question_ref
from .goals import AttenuationError, QuotaExceededError
from .notification import COMPLETION_MAX_CHARS
from .quota_guard import QuotaGuard
from .views import (DRAFT_LIMIT, TRANSITION_LIMIT, actionable_notifications,
                    awaiting_acceptances, bounded_latest, collection,
                    completion_id, event_counts, event_summaries,
                    is_open_question, notification_counts, notification_detail,
                    notification_selection, notification_summary,
                    request_content, request_counts, request_detail,
                    request_ref, transition_summary)
from .visualizer import render_html, render_text_tree

log = logging.getLogger(__name__)

_SCAN_CONNECTION_POLL_SECONDS = 1.0

_ROLE_FORBIDDEN_TOOLS = {
    'root': frozenset({'terminal', 'execute_code', 'write_file', 'patch', 'delegate_task', 'clarify'}),
    'goal': frozenset({'clarify', 'send_message', 'delegate_task'}),
    'judge': frozenset({'terminal', 'execute_code', 'write_file', 'patch', 'clarify', 'send_message', 'delegate_task'}),
}


class ToolProtocolError(ValueError):
    """A provider emitted the wrong outer shape for a tool call."""


def _normalize_tool_arguments(name, args):
    if not isinstance(args, dict) or 'calls' not in args:
        return args
    unknown = set(args) - {'calls', 'name'}
    if unknown:
        raise ToolProtocolError('tool-call wrapper contains unsupported fields: '
                                + ', '.join(sorted(unknown)))
    calls = args.get('calls')
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise ToolProtocolError('tool-call wrapper must contain exactly one call object')
    wrapped_name = args.get('name')
    call = calls[0]
    call_name = call.get('name')
    if not wrapped_name and not call_name:
        raise ToolProtocolError('tool-call wrapper must identify the target tool by name')
    if wrapped_name not in {None, name}:
        raise ToolProtocolError(f'tool-call wrapper names {wrapped_name!r}, expected {name!r}')
    if call_name not in {None, name}:
        raise ToolProtocolError(f'nested tool-call names {call_name!r}, expected {name!r}')
    if 'arguments' in call:
        unknown = set(call) - {'name', 'arguments'}
        if unknown:
            raise ToolProtocolError('nested tool-call wrapper contains unsupported fields: '
                                    + ', '.join(sorted(unknown)))
        nested = call['arguments']
    else:
        nested = {k: v for k, v in call.items() if k not in {'name', 'arguments'}}
    if not isinstance(nested, dict):
        raise ToolProtocolError('nested tool-call arguments must be an object')
    return dict(nested)


def _missing_fields(args, fields):
    missing = []
    for field in fields:
        value = args.get(field)
        if (field not in args or value is None
                or (isinstance(value, str) and not value.strip())
                or (isinstance(value, (list, dict)) and not value)):
            missing.append(field)
    return missing


def _invalid_request(name, *, missing=(), reason=None, next_step=None):
    missing = list(missing)
    result = {
        'error': reason or f'{name} is missing required fields: {", ".join(missing)}',
        'code': 'invalid_request',
        'tool': name,
    }
    if missing:
        result['missing_fields'] = missing
    result['next_step'] = next_step or f'Provide {", ".join(missing)} and retry {name} directly.'
    return result


def _resolve_role(ctx) -> str | None:
    """Resolve ANCOR role from 2-profile config (default vs ancor-exec + ANCOR_ROLE)."""
    interactor_profile = ctx.get_config('interactor_profile', 'default')
    exec_profile = ctx.get_config('exec_profile', 'ancor-exec')
    env_role = (os.environ.get('ANCOR_ROLE') or '').strip().lower()

    if ctx.profile_name == exec_profile:
        if env_role in {'root', 'goal', 'judge'}:
            return env_role
        if os.environ.get('GOAL_ID') or os.environ.get('ANCOR_GOAL_ID'):
            return 'goal'
        return 'root'

    if ctx.profile_name == interactor_profile:
        return 'interactor'
    return None


def _role_fence_hook(role: str):
    forbidden = _ROLE_FORBIDDEN_TOOLS.get(role, frozenset())

    def pre_tool_call(tool_name, args=None, **kwargs):
        if tool_name in forbidden:
            return {
                'action': 'block',
                'message': f'ANCOR role {role!r} is not permitted to call tool {tool_name!r}.',
            }
        return None

    return pre_tool_call


def _observed_model_hook(sink: list, *, on_new=None):
    def post_api_request(model=None, provider=None, response_model=None, **_kwargs):
        name = (model or response_model or '').strip() if isinstance(model or response_model, str) else ''
        if not name:
            return None
        entry = {'model': name, 'provider': (provider or None) if isinstance(provider, str) else None}
        if entry in sink:
            return None
        sink.append(entry)
        if on_new is not None:
            try:
                on_new(entry['model'], entry['provider'])
            except Exception:
                log.exception('ANCOR failed to record observed model %s', entry)
        return None

    return post_api_request


def register(ctx, *, scan_runner=None, judge_runner=None):
    if scan_runner is None:
        from .host import scan_runner

    owner = (ctx.profile_name == ctx.get_config('owner_profile', 'default')
             and ctx.get_config('gateway_owner', False)
             and not os.environ.get('ANCOR_ROLE'))
    if owner:
        def connected(native, adapter):
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

    role = _resolve_role(ctx)
    if role is None:
        return

    interactor = role == 'interactor'
    root_role = role == 'root'
    goal_role = role == 'goal'
    exec_profile = ctx.get_config('exec_profile', 'ancor-exec')

    if interactor and hasattr(ctx, 'register_locale'):
        from agent.i18n import supported_languages
        for language in supported_languages():
            ctx.register_locale(language, {'gateway.clarify.skip_hint': ''})

    from .service import Service
    service = Service(ctx)
    observed_models: list[dict] = []

    def outbox():
        from .notification import NotificationOutbox
        return NotificationOutbox(
            service.store.path + '.notifications.db',
            coordinator_profile=exec_profile,
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
            'request_id': row.get('request_id'),
            'reference': row['answer_reference'],
            'source': ('native_clarify_response' if confirmed else 'interactor_transcription'),
            'confirmed': confirmed,
            'authorization': False},
            source_key='notification.response:' + row['id'])

    def notifications():
        rows = outbox().list()
        for row in rows:
            if row['answer'] is not None:
                response_event(row)
        return rows

    def ack_events(args, **_native):
        event_ids = args.get('event_ids')
        if (not isinstance(event_ids, list) or not event_ids
                or any(type(i) is not int or i < 1 for i in event_ids)):
            raise ValueError('event_ids must contain positive integer IDs')
        retained = service.store.events_by_ids(event_ids)
        by_id = {event['id']: event for event in retained}
        missing = [i for i in event_ids if i not in by_id]
        if missing:
            return {'acked': [], 'already_acked': [],
                    'rejected': [{'id': i, 'reason': 'not_found'} for i in missing],
                    'remaining_pending': len(service.store.pending_events())}
        acked, already = [], []
        for i in dict.fromkeys(event_ids):
            if by_id[i]['acknowledged']:
                already.append(i)
            else:
                acked.append(i)
        if acked:
            service.store.ack_events(acked)
        result = {'acked': acked, 'already_acked': already, 'rejected': [],
                  'remaining_pending': len(service.store.pending_events())}
        if args.get('reason') is not None:
            result['reason'] = args['reason']
        return result

    def get_context(args, **_native):
        from .host import goal_summary
        request_id = args.get('request_id')
        event_id = args.get('event_id')
        include_content = bool(args.get('include_content', False))
        include_history = bool(args.get('include_history', False))
        include_closed = bool(args.get('include_closed_notifications', False))
        notification_limit = args.get('notification_limit', 10)
        before_id = args.get('before_id')

        if event_id is not None:
            if not root_role:
                raise PermissionError('Root Agent role required to inspect events')
            retained = service.store.events_by_ids([event_id])
            if not retained:
                raise KeyError(event_id)
            return {'event': retained[0]}

        try:
            notification_rows = notifications()
        except ValueError as exc:
            notification_rows = []
            notification_error = str(exc)
        else:
            notification_error = None
        extra = {'notifications_error': notification_error} if notification_error else {}
        data = service.store.snapshot()
        pending = data['pending_events']

        if request_id:
            request = service.store.get_request(request_id)
            goals = service.goal_store.list_goals(request_id=request_id, include_terminal=True)
            verdicts = service.judge_gate.list_verdicts(request_id)
            relevant = [row for row in notification_rows if row.get('request_id') == request_id]
            eligible = relevant if include_closed else actionable_notifications(relevant)
            selected = notification_selection(eligible, notification_limit)
            content = request_content(service.store.content(request_id), request['brief'],
                                      expanded=include_content)
            if not include_content:
                content['exact_via'] = {'tool': 'ancor_request_get',
                                        'args': {'request_id': request_id, 'include_content': True}}
            grants = service.store.active_grants(request_id)
            result = {
                'request': request_detail(request, include_text=False, include_audit=include_history),
                'content': content,
                'grants': (collection([{k: g[k] for k in ('id', 'action', 'target', 'scope')}
                                       for g in grants]) if include_content else {'count': len(grants)}),
                'goals': collection([goal_summary(g) for g in goals]),
                'judge_verdicts': collection(verdicts[-5:], total=len(verdicts)),
                'questions': collection([question_ref(q)
                                         for q in service.store.questions(request_id) if q['state'] == 'open']),
                'notifications': collection(
                    [notification_summary(row, exact_via=True) for row in selected],
                    total=len(eligible)),
                'counts': {'notifications': notification_counts(relevant), 'goals': len(goals)},
                **extra,
            }
            transitions = service.store.request_transitions(request_id)
            if transitions:
                result['transitions'] = (
                    bounded_latest([transition_summary(e) for e in transitions], TRANSITION_LIMIT, before_id=before_id)
                    if include_history else
                    {'count': len(transitions),
                     'exact_via': {'tool': 'ancor_request_get', 'args': {'request_id': request_id, 'include_history': True}}}
                )
            if root_role:
                req_events = [e for e in pending if (e.get('payload') or {}).get('request_id') == request_id]
                result['pending_events'] = collection(event_summaries(req_events, 50, exact_via=True), total=len(req_events))
            return result

        open_requests = [
            r for r in data['requests']
            if r['status'] in {'active', 'paused', 'pending'} or r.get('target_status') is not None
        ]
        active_goals = service.goal_store.list_goals(include_terminal=False)
        reviews = awaiting_acceptances(data['requests'], notification_rows)
        notification_source = notification_rows if include_closed else actionable_notifications(notification_rows)
        selected = notification_selection(notification_source, notification_limit)
        result = {
            'requests': collection([request_ref(r, exact_via=True) for r in open_requests]),
            'active_goals': collection([goal_summary(g) for g in active_goals]),
            'resources': {
                **service.goal_store.total_active_usage(),
                'backend': service.cluster_backend.summary(),
            },
            'awaiting_acceptance': collection(reviews),
            'counts': {
                'requests': request_counts(data['requests']),
                'active_goals': len(active_goals),
                'events': event_counts(pending),
                'notifications': notification_counts(notification_rows),
                'awaiting_acceptance': len(reviews),
            },
            'notifications': collection(
                [notification_summary(row, exact_via=True) for row in selected],
                total=len(notification_source)),
            **extra,
        }
        if interactor:
            confirmed = {r['draft_id'] for r in data['requests']}
            drafts = [d for d in data['drafts'] if d['id'] not in confirmed]
            result['drafts'] = collection(drafts[:DRAFT_LIMIT], total=len(drafts))
        if root_role:
            result['pending_events'] = collection(event_summaries(pending, 100, exact_via=True), total=len(pending))
        else:
            result['pending_events'] = collection([], total=len(pending))
        return result

    def visualize(args, **_native):
        req_id = args.get('request_id')
        tree_text = render_text_tree(service.store.path, request_id=req_id)
        out = {'tree': tree_text}
        output_path = args.get('output_path')
        if output_path:
            target = Path(output_path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_html(service.store.path, request_id=req_id), encoding='utf-8')
            out['html_path'] = str(target)
        return out

    def notify(args, **_native):
        action = args['action']
        if action == 'list':
            rows = notifications()
            identifier = args.get('id')
            if identifier:
                rows = [row for row in rows if row['id'] == identifier]
                if not rows:
                    raise KeyError(identifier)
            if args.get('state'):
                rows = [row for row in rows if row['state'] == args['state']]
            if args.get('request_id'):
                rows = [row for row in rows if row.get('request_id') == args['request_id']]
            if args.get('open_only'):
                rows = [row for row in rows if is_open_question(row)]
            limit = args.get('limit', 50)
            total = len(rows)
            rows = rows[-limit:]
            items = ([notification_detail(row) for row in rows] if identifier
                     else [notification_summary(row) for row in rows])
            return {'notifications': collection(items, total=total)}
        box = outbox()
        identifier = args['id']
        if action == 'send':
            if not root_role:
                raise PermissionError('Root Agent role required to send notifications')
            if args.get('kind') != 'completion':
                raise ValueError('Use ancor_question_create for questions; ancor_completion_send is for verified completions')
            req = service.store.get_request(args['request_id'])
            if req['status'] != 'awaiting_acceptance':
                raise ValueError('completion requires a request awaiting human acceptance')
            if identifier != completion_id(req):
                raise ValueError('completion id must be ' + completion_id(req))
            if len(args['message']) > COMPLETION_MAX_CHARS:
                raise ValueError(f'completion message exceeds {COMPLETION_MAX_CHARS} characters')
            box.enqueue(identifier, 'completion', args['message'],
                        actor=exec_profile, request_id=args['request_id'])
            return notification_detail(box.get(identifier))
        if action == 'respond':
            if not interactor:
                raise PermissionError('interactor role required to respond')
            existing = box.get(identifier)
            if existing['kind'] != 'question':
                raise ValueError('not a question')
            if existing['answer'] is not None:
                if existing['answer'] != args['answer']:
                    raise ValueError('question already answered')
                event = response_event(existing)
                return {'notification': notification_detail(existing),
                        'event': event, 'confirmed': event['payload']['confirmed']}
            proposal = box.propose_response(identifier, args['answer'])
            return {'notification': notification_detail(existing),
                    **service.prepared_response_presentation(proposal)}
        raise ValueError('unknown notification action')

    def _caller_goal_id():
        if root_role:
            return 'root'
        gid = (os.environ.get('GOAL_ID') or os.environ.get('ANCOR_GOAL_ID') or '').strip()
        if not gid:
            raise PermissionError('GOAL_ID environment variable is required for non-root goal operations')
        return gid

    def _job_canceller(handle):
        return service.cluster_backend.cancel_handle(handle['handle_id'], handle.get('tags'))

    def goal_list(args, **_native):
        goals = service.goal_store.list_goals(
            request_id=args.get('request_id'),
            parent_goal_id=args.get('parent_goal_id'),
            status=args.get('status'),
            include_terminal=bool(args.get('include_terminal', False)),
        )
        return collection(goals)

    def goal_get(args, **_native):
        gid = args['goal_id']
        goal = service.goal_store.get_goal(gid)
        children = service.goal_store.list_goals(parent_goal_id=gid, include_terminal=True)
        return {'goal': goal, 'children': collection(children),
                'subtree_active_usage': service.goal_store.subtree_active_usage(gid)}

    def goal_self(_args, **_native):
        gid = _caller_goal_id()
        goal = service.goal_store.get_goal(gid)
        children = service.goal_store.list_goals(parent_goal_id=gid, include_terminal=True)
        return {'goal': goal, 'children': collection(children),
                'subtree_active_usage': service.goal_store.subtree_active_usage(gid)}

    def goal_read_request(args, **_native):
        gid = _caller_goal_id()
        return service.goal_store.read_request_for_goal(gid, args['request_id'])

    def goal_spawn(args, **_native):
        caller = _caller_goal_id()
        spec = args['goal_yaml'] if args.get('goal_yaml') else args
        return service.goal_store.spawn_goal(spec, caller_goal_id=caller)

    def goal_signal(args, **_native):
        caller = _caller_goal_id()
        payload = {}
        if args.get('message') is not None:
            payload['message'] = args['message']
        if args.get('generation') is not None:
            payload['generation'] = args['generation']
        return service.goal_store.signal_goal(
            args['goal_id'], args['kind'], payload, caller_goal_id=caller)

    def goal_pause(args, **_native):
        caller = _caller_goal_id()
        return service.goal_store.pause_goal(
            args['goal_id'], mode=args.get('mode', 'drain'),
            caller_goal_id=caller, job_canceller=_job_canceller)

    def goal_resume(args, **_native):
        caller = _caller_goal_id()
        return service.goal_store.resume_goal(args['goal_id'], caller_goal_id=caller)

    def goal_stop(args, **_native):
        caller = _caller_goal_id()
        return service.goal_store.stop_goal(
            args['goal_id'], reason=args.get('reason', 'stopped'),
            caller_goal_id=caller, job_canceller=_job_canceller)

    def goal_report(args, **_native):
        gid = _caller_goal_id()
        return service.goal_store.report_result(gid, {'goal_id': gid, **args})

    def resource_acquire(args, **_native):
        gid = _caller_goal_id()
        guard = QuotaGuard(service.goal_store, service.cluster_backend)
        return guard.acquire(
            usage=args.get('usage'), tags=args.get('tags'),
            handle_id=args.get('handle_id'), command=args.get('command'),
            goal_id=gid, metadata=args.get('metadata'))

    def cluster_status(_args, **_native):
        return {
            **service.goal_store.total_active_usage(),
            'backend': service.cluster_backend.summary(),
        }

    def delivery_submit(args, **_native):
        packet = service.judge_gate.build_judge_packet(
            args['request_id'],
            summary=args['summary'],
            artifacts=args['artifacts'],
            eval_evidence=args.get('eval_evidence'),
        )
        if judge_runner is not None:
            verdict = judge_runner(packet)
            return {'packet': packet, 'verdict': verdict}
        event = service.store.add_event('delivery.submitted', {
            'request_id': args['request_id'],
            'generation': packet['request']['generation'],
            'summary': args['summary'],
            'artifacts': args['artifacts'],
            'judge_packet': packet,
        })
        return {'status': 'submitted_to_judge', 'event_id': event['id'], 'judge_packet': packet}

    def judge_read_request(args, **_native):
        req = service.store.get_request(args['request_id'])
        content = service.store.content(args['request_id'])
        grants = service.store.active_grants(args['request_id'])
        return {
            'request': request_detail(req, include_text=True, include_audit=False),
            'content': content,
            'grants': [{k: g[k] for k in ('id', 'action', 'target', 'scope')} for g in grants],
        }

    def judge_verdict(args, **_native):
        return service.judge_gate.apply_verdict(
            args['request_id'],
            generation=args['generation'],
            decision=args['decision'],
            summary=args['summary'],
            evidence=args.get('evidence'),
            feedback=args.get('feedback'),
            artifacts=args.get('artifacts'),
            actor='ancor-judge',
            runtime=list(observed_models),
        )

    methods = {
        'request': service.request,
        'get_context': get_context,
        'visualize': visualize,
        'ack_events': ack_events,
        'notify': notify,
        'note': service.note,
        'signal': service.signal,
        'goal_list': goal_list,
        'goal_get': goal_get,
        'goal_self': goal_self,
        'goal_read_request': goal_read_request,
        'goal_spawn': goal_spawn,
        'goal_signal': goal_signal,
        'goal_pause': goal_pause,
        'goal_resume': goal_resume,
        'goal_stop': goal_stop,
        'goal_report': goal_report,
        'resource_acquire': resource_acquire,
        'cluster_status': cluster_status,
        'delivery_submit': delivery_submit,
        'judge_read_request': judge_read_request,
        'judge_verdict': judge_verdict,
    }

    change_properties = {}
    if interactor or root_role:
        atomic_defs, atomic_methods = atomic_interfaces(
            service, interactor=interactor, coordinator=root_role)
        change_properties = atomic_defs['ancor_change'][1]
        methods.update(atomic_methods)

    if interactor:
        ctx.register_hook('pre_tool_call', service.pre_tool_call)
        ctx.register_hook('post_tool_call', service.post_tool_call)
    else:
        ctx.register_hook('pre_tool_call', _role_fence_hook(role))
        # Record the model that actually answered (post-fallback) straight from Hermes;
        # never self-reported by the agent.
        ctx.register_hook('post_api_request', _observed_model_hook(
            observed_models,
            on_new=(lambda model, provider: service.goal_store.record_observed_model(
                _caller_goal_id(), model, provider)) if goal_role else None))

    from .tool_surface import ROOT_MUTATION_TOOLS, build_surface
    definitions, handlers = build_surface(
        role=role, methods=methods, change_properties=change_properties)

    for name, (description, properties, required) in definitions.items():
        method = handlers[name.removeprefix('ancor_')]
        try:
            accepts_native_kwargs = any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            accepts_native_kwargs = False

        def handle(args, _method=method, _name=name, _properties=properties,
                   _required=required, _accepts_native_kwargs=accepts_native_kwargs, **kwargs):
            try:
                args = _normalize_tool_arguments(_name, args)
                if _name in ROOT_MUTATION_TOOLS and not (root_role or (goal_role and _name.startswith('ancor_goal_'))):
                    raise PermissionError('Root Agent role required')
                missing = _missing_fields(args, _required)
                if missing:
                    return json.dumps(_invalid_request(_name, missing=missing), ensure_ascii=False)
                unknown = set(args) - set(_properties)
                if unknown:
                    raise ValueError('unsupported arguments: ' + ', '.join(sorted(unknown)))
                result = (_method(args, **kwargs) if _accepts_native_kwargs else _method(args))
                return json.dumps(result, ensure_ascii=False)
            except ToolProtocolError as exc:
                return json.dumps({'error': str(exc), 'code': 'protocol_error', 'tool': _name}, ensure_ascii=False)
            except (AttenuationError, QuotaExceededError) as exc:
                code = 'attenuation_violation' if isinstance(exc, AttenuationError) else 'quota_exceeded'
                return json.dumps({'error': str(exc), 'code': code, 'tool': _name}, ensure_ascii=False)
            except Exception as exc:
                log.exception('ANCOR tool %s failed', _name)
                code = ('forbidden' if isinstance(exc, PermissionError) else
                        'not_found' if isinstance(exc, KeyError) else
                        'invalid_request' if isinstance(exc, ValueError) else
                        'state_conflict' if isinstance(exc, RuntimeError) else 'internal')
                return json.dumps({'error': str(exc), 'code': code}, ensure_ascii=False)

        ctx.register_tool(
            name=name, toolset='ancor',
            schema={'name': name, 'description': description, 'parameters': {
                'type': 'object', 'properties': properties, 'required': required,
                'additionalProperties': False}},
            handler=handle,
        )
