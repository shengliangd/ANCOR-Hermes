"""Per-role MCP stdio server exposing ANCOR's native tool surface to any CLI backend.

Launched by ExecutionBackend adapters (e.g. Codex, Hermes, AGY, DSH) as:
    ancor mcp --role <root|goal|judge|interactor> [--goal-id <id>] [--config <path>]

Role and goal_id are frozen at process launch so the model cannot escalate privileges
or spoof another goal's identity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from .config import AncorConfig, load_config
from .core_api import AncorCore
from .goals import AttenuationError, QuotaExceededError
from .tool_surface import build_surface

log = logging.getLogger(__name__)


def build_role_tools(
    core: AncorCore,
    *,
    role: str,
    goal_id: str | None = None,
) -> tuple[dict[str, tuple[str, dict, list[str]]], dict[str, Any]]:
    """Build (definitions, handlers) for a single frozen ANCOR role using tool_surface.py."""
    if role not in {'root', 'goal', 'judge', 'interactor'}:
        raise ValueError(f'unsupported ANCOR MCP role {role!r}')
    if role == 'goal' and not (goal_id and goal_id.strip()):
        raise ValueError('goal_id is required when role="goal"')

    caller_id = 'root' if role == 'root' else (goal_id or '')

    def _get_context(args):
        if args.get('event_id') is not None:
            if role != 'root':
                raise PermissionError('Root Agent role required to inspect events')
            return core.event_get(args['event_id'])
        if args.get('request_id'):
            return core.request_get(
                args['request_id'],
                include_content=bool(args.get('include_content', False)),
                include_history=bool(args.get('include_history', False)),
                include_events=(role == 'root'),
                include_closed_notifications=bool(args.get('include_closed_notifications', False)),
                notification_limit=args.get('notification_limit', 10),
                before_id=args.get('before_id'),
            )
        return core.overview(include_events=(role == 'root'))

    def _question(args):
        action = args['action']
        if action == 'list':
            return core.question_list(
                request_id=args.get('request_id'),
                include_closed=bool(args.get('include_closed', False)),
                limit=args.get('limit', 50),
            )
        if action == 'get':
            return core.question_get(args['id'], request_id=args.get('request_id'))
        if action == 'create':
            if role != 'root':
                raise PermissionError('Root Agent role required')
            return core.question_create(
                request_id=args['request_id'],
                question_id=args['id'],
                body=args['body'],
                reason=args['reason'],
                point_ids=args.get('point_ids') or [],
                actor='root',
            )
        if action == 'withdraw':
            if role != 'root':
                raise PermissionError('Root Agent role required')
            return core.question_withdraw(args['id'], reason=args['reason'], actor='root')
        raise ValueError(f'unknown question action {action!r}')

    def _change(args):
        action = args['action']
        if action == 'get':
            return core.change_get(args['change_id'])
        if action == 'history':
            return core.change_history(
                args['request_id'],
                before=args.get('before'),
                limit=args.get('limit', 50),
                include_notes=(role == 'root'),
            )
        if action == 'propose':
            if role != 'interactor':
                raise PermissionError('interactor role required')
            return core.change_propose(
                request_id=args.get('request_id'),
                operations=args['operations'],
                input_text=args['input'],
                base=args.get('base'),
                actor='interactor',
            )
        raise ValueError(f'unknown change action {action!r}')

    def _request(args):
        action = args['action']
        if action == 'advance':
            if role != 'root':
                raise PermissionError('Root Agent role required')
            return core.request_advance(args['request_id'])
        if role != 'interactor':
            raise PermissionError('interactor role required')
        if action == 'pause':
            return core.request_pause(args['request_id'], reason=args['reason'], actor='interactor')
        if action == 'resume':
            return core.request_resume(args['request_id'], reason=args['reason'], actor='interactor')
        if action == 'cancel':
            results = [
                core.request_cancel(rid, reason=args.get('reason') or 'cancelled', actor='interactor')
                for rid in args['request_ids']
            ]
            return {'cancelled': results}
        if action == 'accept':
            return core.request_accept(args['request_id'], actor='interactor')
        if action == 'request_changes':
            return core.request_changes(args['request_id'], feedback=args['feedback'], actor='interactor')
        raise ValueError(f'unknown request action {action!r}')

    def _notify(args):
        action = args['action']
        if action == 'list':
            return core.notification_list(
                notification_id=args.get('id'),
                request_id=args.get('request_id'),
                state=args.get('state'),
                open_only=bool(args.get('open_only', False)),
                limit=args.get('limit', 50),
            )
        if action == 'send':
            if role != 'root':
                raise PermissionError('Root Agent role required')
            return core.notification_send_completion(
                notification_id=args['id'],
                request_id=args['request_id'],
                message=args['message'],
                actor='root',
            )
        if action == 'respond':
            if role != 'interactor':
                raise PermissionError('interactor role required')
            return core.notification_respond(args['id'], answer=args['answer'], actor='interactor')
        raise ValueError(f'unknown notification action {action!r}')

    def _note(args):
        action = args['action']
        rid = args['request_id']
        if action == 'list':
            return core.note_list(rid, limit=args.get('limit', 50), include_deleted=bool(args.get('include_deleted', False)))
        if action == 'get':
            return core.note_get(rid, args['note_id'])
        if action == 'history':
            return core.note_history(rid, args['note_id'])
        if role != 'root':
            raise PermissionError('Root Agent role required')
        if action == 'add':
            return core.note_add(rid, args['body'], actor='root')
        if action == 'update':
            return core.note_update(rid, args['note_id'], args['body'], expected_revision=args['expected_revision'], actor='root')
        if action == 'delete':
            return core.note_delete(rid, args['note_id'], expected_revision=args['expected_revision'], actor='root')
        raise ValueError(f'unknown note action {action!r}')

    def _resource_release(args):
        if role == 'root':
            gid = args.get('goal_id')
            if not gid:
                raise ValueError('goal_id is required when releasing resources as root')
        else:
            gid = caller_id
            if args.get('goal_id') and args['goal_id'] != gid:
                raise PermissionError(f"goal {gid} cannot release resources owned by {args['goal_id']}")
        return core.resource_release(
            gid,
            handle_id=args.get('handle_id'),
            status=args.get('status', 'released'),
            detail=args.get('detail'),
        )

    methods = {
        'get_context': _get_context,
        'visualize': lambda args: core.visualize(request_id=args.get('request_id'), output_path=args.get('output_path')),
        'ack_events': lambda args: core.event_ack(args['event_ids'], reason=args.get('reason')),
        'notify': _notify,
        'note': _note,
        'question': _question,
        'change': _change,
        'request': _request,
        'signal': lambda args: core.signal_send(args['text'], actor=role),
        'goal_list': lambda args: core.goal_list(
            request_id=args.get('request_id'),
            parent_goal_id=args.get('parent_goal_id'),
            status=args.get('status'),
            include_terminal=bool(args.get('include_terminal', False)),
        ),
        'goal_get': lambda args: core.goal_get(args['goal_id']),
        'goal_self': lambda _args: core.goal_get(caller_id),
        'goal_read_request': lambda args: core.request_read(args['request_id'], goal_id=caller_id),
        'goal_spawn': lambda args: core.goal_spawn(
            args['goal_yaml'] if args.get('goal_yaml') else args, caller_goal_id=caller_id),
        'goal_signal': lambda args: core.goal_signal(
            args['goal_id'], args['kind'], message=args.get('message'),
            generation=args.get('generation'), caller_goal_id=caller_id),
        'goal_pause': lambda args: core.goal_pause(
            args['goal_id'], mode=args.get('mode', 'drain'), caller_goal_id=caller_id),
        'goal_resume': lambda args: core.goal_resume(args['goal_id'], caller_goal_id=caller_id),
        'goal_stop': lambda args: core.goal_stop(
            args['goal_id'], reason=args.get('reason', 'stopped'), caller_goal_id=caller_id),
        'goal_wait': lambda args: core.goal_wait(
            caller_id,
            wake_after_seconds=args.get('wake_after_seconds'),
            wake_at=args.get('wake_at'),
            watch_path=args.get('watch_path'),
            reason=args.get('reason', ''),
            handoff=args.get('handoff'),
            checklist=args.get('checklist'),
        ),
        'goal_handoff': lambda args: core.goal_handoff(
            caller_id,
            handoff=args['handoff'],
            checklist=args.get('checklist'),
        ),
        'goal_update_budget': lambda args: core.goal_update_budget(
            args['goal_id'], args['budget'], reason=args.get('reason'), caller_goal_id=caller_id),
        'goal_report': lambda args: core.goal_report(caller_id, args),
        'resource_acquire': lambda args: core.resource_acquire(
            caller_id,
            usage=args.get('usage'),
            tags=args.get('tags'),
            handle_id=args.get('handle_id'),
            command=args.get('command'),
            metadata=args.get('metadata'),
        ),
        'resource_release': _resource_release,
        'cluster_status': lambda _args: core.cluster_status(),
        'delivery_submit': lambda args: core.delivery_submit(
            args['request_id'],
            summary=args['summary'],
            artifacts=args['artifacts'],
            eval_evidence=args.get('eval_evidence'),
        ),
        'judge_read_request': lambda args: core.request_read(args['request_id']),
        'judge_verdict': lambda args: core.judge_verdict(
            request_id=args['request_id'],
            delivery_event_id=args.get('delivery_event_id'),
            generation=args['generation'],
            decision=args['decision'],
            summary=args['summary'],
            evidence=args.get('evidence'),
            feedback=args.get('feedback'),
            artifacts=args.get('artifacts'),
            actor='ancor-judge',
        ),
    }

    change_properties = {}
    if role == 'interactor':
        from .atomic_tools import interfaces as atomic_interfaces
        atomic_defs, _ = atomic_interfaces(None, interactor=True, coordinator=False)
        change_properties = atomic_defs['ancor_change'][1]

    definitions, handlers = build_surface(role=role, methods=methods, change_properties=change_properties)

    # Shift-based ephemeral execution schemas for goal role (handoff + checklist on wait & handoff).
    if role == 'goal':
        handoff_schema = {
            'type': 'object',
            'description': 'Bounded (<= 3KB) shift handoff ledger entry for the next fresh session.',
            'properties': {
                'verified_progress': {
                    'oneOf': [
                        {'type': 'string', 'minLength': 1},
                        {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
                    ]
                },
                'dead_ends': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
                'next_step': {'type': 'string', 'minLength': 1},
                'artifact_refs': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
            },
            'required': ['verified_progress', 'next_step'],
            'additionalProperties': False,
        }
        checklist_schema = {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string', 'minLength': 1},
                    'item': {'type': 'string', 'minLength': 1},
                    'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'done', 'skipped']},
                    'verify_hint': {'type': 'string'},
                    'evidence': {'type': 'string'},
                },
                'required': ['id', 'item', 'status'],
                'additionalProperties': False,
            },
        }
        if 'ancor_goal_wait' in definitions:
            w_desc, w_props, w_req = definitions['ancor_goal_wait']
            definitions['ancor_goal_wait'] = (
                w_desc + ' Optionally persist handoff and checklist atomically in the same transaction.',
                {**w_props, 'handoff': handoff_schema, 'checklist': checklist_schema},
                w_req,
            )
        definitions['ancor_goal_handoff'] = (
            'Persist a concise shift handoff (<= 3KB) and optional checklist, discard the current session_id, '
            'and schedule a fresh shift to prevent context rot. End this turn immediately after calling.',
            {'handoff': handoff_schema, 'checklist': checklist_schema},
            ['handoff'],
        )
        handlers['goal_handoff'] = methods['goal_handoff']

    # Cross-backend ANCOR Memory tools (role-scoped read/write; judge is read-only & cannot see episodes).
    definitions['ancor_memory_index'] = (
        'List the auto-generated ANCOR memory index (environment facts, playbooks, and episodes).',
        {
            'scope': {'type': 'string'},
            'category': {'type': 'string', 'enum': ['principles', 'environment', 'playbooks', 'episodes']},
        },
        [],
    )
    handlers['memory_index'] = lambda args: core.memory_index(
        role=role, scope=args.get('scope'), category=args.get('category'))

    definitions['ancor_memory_read'] = (
        'Read one ANCOR memory document by relative path (e.g. "environment/topology.md", "playbooks/external-jobs.md").',
        {'path': {'type': 'string', 'minLength': 1}},
        ['path'],
    )
    handlers['memory_read'] = lambda args: core.memory_read(args['path'], role=role)

    definitions['ancor_memory_search'] = (
        'Full-text search across ANCOR Markdown memory documents.',
        {
            'query': {'type': 'string', 'minLength': 1},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
        },
        ['query'],
    )
    handlers['memory_search'] = lambda args: core.memory_search(
        args['query'], role=role, limit=args.get('limit', 10))

    if role in {'root', 'goal'}:
        definitions['ancor_memory_write'] = (
            'Atomically create or update an agent-writable ANCOR memory file under environment/, playbooks/, or episodes/. '
            'Pass expected_hash when updating an existing file to prevent concurrent write clobbering.',
            {
                'path': {'type': 'string', 'minLength': 1},
                'content': {'type': 'string', 'minLength': 1},
                'expected_hash': {'type': 'string'},
                'scope': {'type': 'string'},
                'summary': {'type': 'string'},
                'verified_at': {'type': 'string'},
            },
            ['path', 'content'],
        )
        handlers['memory_write'] = lambda args: core.memory_write(
            args['path'],
            args['content'],
            role=role,
            expected_hash=args.get('expected_hash'),
            scope=args.get('scope'),
            summary=args.get('summary'),
            verified_at=args.get('verified_at'),
        )

    return definitions, handlers


def execute_role_tool(
    definitions: dict,
    handlers: dict,
    name: str,
    arguments: dict | None,
) -> str:
    """Validate and invoke one ANCOR tool, returning a JSON string."""
    if name not in definitions:
        return json.dumps({'error': f'tool {name!r} is not authorized for this role', 'code': 'forbidden'})
    _desc, properties, required = definitions[name]
    args = dict(arguments or {})
    missing = [
        f for f in required
        if f not in args or args[f] is None
        or (isinstance(args[f], str) and not args[f].strip())
        or (isinstance(args[f], (list, dict)) and not args[f])
    ]
    if missing:
        return json.dumps({
            'error': f'{name} is missing required fields: {", ".join(missing)}',
            'code': 'invalid_request',
            'missing_fields': missing,
            'tool': name,
        }, ensure_ascii=False)
    unknown = set(args) - set(properties)
    if unknown:
        return json.dumps({
            'error': 'unsupported arguments: ' + ', '.join(sorted(unknown)),
            'code': 'invalid_request',
            'tool': name,
        }, ensure_ascii=False)
    handler = handlers[name.removeprefix('ancor_')]
    try:
        result = handler(args)
        return json.dumps(result, ensure_ascii=False)
    except (AttenuationError, QuotaExceededError) as exc:
        code = 'attenuation_violation' if isinstance(exc, AttenuationError) else 'quota_exceeded'
        return json.dumps({'error': str(exc), 'code': code, 'tool': name}, ensure_ascii=False)
    except Exception as exc:
        log.exception('ANCOR MCP tool %s failed', name)
        code = (
            'forbidden' if isinstance(exc, PermissionError) else
            'not_found' if isinstance(exc, KeyError) else
            'invalid_request' if isinstance(exc, ValueError) else
            'state_conflict' if isinstance(exc, RuntimeError) else 'internal'
        )
        return json.dumps({'error': str(exc), 'code': code, 'tool': name}, ensure_ascii=False)


def create_mcp_server(
    *,
    role: str,
    goal_id: str | None = None,
    config: AncorConfig | None = None,
    core: AncorCore | None = None,
):
    from mcp.server.lowlevel import Server
    from mcp import types

    ancor_core = core or AncorCore(config or load_config())
    definitions, handlers = build_role_tools(ancor_core, role=role, goal_id=goal_id)

    server = Server(f'ancor-{role}')

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools = []
        for name, (description, properties, required) in definitions.items():
            tools.append(types.Tool(
                name=name,
                description=description,
                inputSchema={
                    'type': 'object',
                    'properties': properties,
                    'required': required,
                    'additionalProperties': False,
                },
            ))
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        payload = await asyncio.to_thread(execute_role_tool, definitions, handlers, name, arguments)
        return [types.TextContent(type='text', text=payload)]

    return server


async def run_mcp_stdio(
    *,
    role: str,
    goal_id: str | None = None,
    config_path: str | Path | None = None,
    store_path: str | Path | None = None,
) -> None:
    from mcp.server.stdio import stdio_server

    cfg = load_config(config_path, store_path_override=store_path)
    server = create_mcp_server(role=role, goal_id=goal_id or os.environ.get('GOAL_ID'), config=cfg)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
