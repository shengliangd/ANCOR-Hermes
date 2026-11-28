"""Machine-facing ``ancor core`` CLI subcommands (JSON in/out, no interactive confirmation)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..config import load_config
from ..core_api import AncorCore
from ..goals import AttenuationError, QuotaExceededError


def _read_json_arg(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if raw == '-':
        text = sys.stdin.read()
    else:
        text = raw
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('--json payload must be a JSON object')
    return data


def _emit(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _emit_error(exc: Exception) -> int:
    code = (
        'attenuation_violation' if isinstance(exc, AttenuationError) else
        'quota_exceeded' if isinstance(exc, QuotaExceededError) else
        'forbidden' if isinstance(exc, PermissionError) else
        'not_found' if isinstance(exc, KeyError) else
        'invalid_request' if isinstance(exc, (ValueError, json.JSONDecodeError)) else
        'state_conflict' if isinstance(exc, RuntimeError) else 'internal'
    )
    print(json.dumps({'error': str(exc), 'code': code}, ensure_ascii=False))
    return 1


def register_core_parser(subparsers) -> None:
    p = subparsers.add_parser(
        'core',
        help='Machine-facing control interface (JSON in/out, assumes caller already confirmed)',
    )
    p.add_argument('--actor', default='ancor-core', help='Audit actor label')
    p.add_argument('--confirmation-ref', default=None, help='Optional upstream confirmation reference')
    sub = p.add_subparsers(dest='core_domain', required=True)

    # overview
    ov = sub.add_parser('overview', help='Compact global ANCOR overview')
    ov.add_argument('--no-events', action='store_true')

    # request
    req = sub.add_parser('request', help='Request operations')
    req_sub = req.add_subparsers(dest='core_action', required=True)
    r_list = req_sub.add_parser('list')
    r_list.add_argument('--include-closed', action='store_true')

    r_get = req_sub.add_parser('get')
    r_get.add_argument('request_id')
    r_get.add_argument('--include-content', action='store_true')
    r_get.add_argument('--include-history', action='store_true')

    r_read = req_sub.add_parser('read')
    r_read.add_argument('request_id')
    r_read.add_argument('--goal-id', default=None)

    r_create = req_sub.add_parser('create')
    r_create.add_argument('--json', dest='json_payload', default=None)
    r_create.add_argument('--title', default=None)
    r_create.add_argument('--outcome', action='append', default=[])
    r_create.add_argument('--constraint', action='append', default=[])
    r_create.add_argument('--advice', action='append', default=[])
    r_create.add_argument('--context', action='append', default=[])

    r_rev = req_sub.add_parser('revise')
    r_rev.add_argument('request_id')
    r_rev.add_argument('--json', dest='json_payload', default=None)

    for act in ('pause', 'resume', 'cancel'):
        rp = req_sub.add_parser(act)
        rp.add_argument('request_id')
        rp.add_argument('--reason', default=act)

    r_acc = req_sub.add_parser('accept')
    r_acc.add_argument('request_id')

    r_chg = req_sub.add_parser('request-changes')
    r_chg.add_argument('request_id')
    r_chg.add_argument('--feedback', required=True)

    r_adv = req_sub.add_parser('advance')
    r_adv.add_argument('request_id')

    # change (two-phase primitives)
    chg = sub.add_parser('change', help='Two-phase change primitives (propose / commit / get / history)')
    chg_sub = chg.add_subparsers(dest='core_action', required=True)
    c_prop = chg_sub.add_parser('propose')
    c_prop.add_argument('--json', dest='json_payload', default=None)
    c_com = chg_sub.add_parser('commit')
    c_com.add_argument('change_id')
    c_get = chg_sub.add_parser('get')
    c_get.add_argument('change_id')
    c_hist = chg_sub.add_parser('history')
    c_hist.add_argument('request_id')
    c_hist.add_argument('--limit', type=int, default=50)

    # goal
    goal = sub.add_parser('goal', help='Goal tree operations')
    g_sub = goal.add_subparsers(dest='core_action', required=True)
    g_list = g_sub.add_parser('list')
    g_list.add_argument('--request-id', default=None)
    g_list.add_argument('--parent-goal-id', default=None)
    g_list.add_argument('--status', default=None)
    g_list.add_argument('--include-terminal', action='store_true')

    g_get = g_sub.add_parser('get')
    g_get.add_argument('goal_id')

    g_spawn = g_sub.add_parser('spawn')
    g_spawn.add_argument('--caller', default='root')
    g_spawn.add_argument('--json', dest='json_payload', default=None)

    g_sig = g_sub.add_parser('signal')
    g_sig.add_argument('goal_id')
    g_sig.add_argument('--kind', required=True, choices=['steer', 'update_generation'])
    g_sig.add_argument('--message', default=None)
    g_sig.add_argument('--generation', type=int, default=None)
    g_sig.add_argument('--caller', default='root')

    g_pause = g_sub.add_parser('pause')
    g_pause.add_argument('goal_id')
    g_pause.add_argument('--mode', default='drain', choices=['drain', 'preempt'])
    g_pause.add_argument('--caller', default='root')

    g_res = g_sub.add_parser('resume')
    g_res.add_argument('goal_id')
    g_res.add_argument('--caller', default='root')

    g_stop = g_sub.add_parser('stop')
    g_stop.add_argument('goal_id')
    g_stop.add_argument('--reason', default='stopped')
    g_stop.add_argument('--caller', default='root')

    g_wait = g_sub.add_parser('wait')
    g_wait.add_argument('goal_id')
    g_wait.add_argument('--wake-after-seconds', type=float, default=None)
    g_wait.add_argument('--wake-at', type=float, default=None)
    g_wait.add_argument('--watch-path', default=None)
    g_wait.add_argument('--reason', default='')
    g_wait.add_argument('--json', dest='json_payload', default=None)

    g_ho = g_sub.add_parser('handoff')
    g_ho.add_argument('goal_id')
    g_ho.add_argument('--json', dest='json_payload', required=True)

    g_sh = g_sub.add_parser('shifts')
    g_sh.add_argument('goal_id')

    g_rep = g_sub.add_parser('report')
    g_rep.add_argument('goal_id')
    g_rep.add_argument('--json', dest='json_payload', default=None)

    # resource
    res = sub.add_parser('resource', help='Resource accounting operations')
    res_sub = res.add_subparsers(dest='core_action', required=True)
    res_sub.add_parser('status')
    res_acq = res_sub.add_parser('acquire')
    res_acq.add_argument('goal_id')
    res_acq.add_argument('--json', dest='json_payload', default=None)
    res_rel = res_sub.add_parser('release')
    res_rel.add_argument('goal_id')
    res_rel.add_argument('--handle-id', default=None)
    res_rel.add_argument('--status', default='released')
    res_rel.add_argument('--detail', default=None)

    # question
    q = sub.add_parser('question', help='Question operations')
    q_sub = q.add_subparsers(dest='core_action', required=True)
    q_list = q_sub.add_parser('list')
    q_list.add_argument('--request-id', default=None)
    q_list.add_argument('--include-closed', action='store_true')
    q_get = q_sub.add_parser('get')
    q_get.add_argument('question_id')
    q_create = q_sub.add_parser('create')
    q_create.add_argument('--json', dest='json_payload', default=None)
    q_ans = q_sub.add_parser('answer')
    q_ans.add_argument('question_id')
    q_ans.add_argument('--body', required=True)
    q_wd = q_sub.add_parser('withdraw')
    q_wd.add_argument('question_id')
    q_wd.add_argument('--reason', required=True)

    # note
    nt = sub.add_parser('note', help='Working note operations')
    nt_sub = nt.add_subparsers(dest='core_action', required=True)
    n_list = nt_sub.add_parser('list')
    n_list.add_argument('request_id')
    n_add = nt_sub.add_parser('add')
    n_add.add_argument('request_id')
    n_add.add_argument('--body', required=True)

    # event
    ev = sub.add_parser('event', help='Event operations')
    ev_sub = ev.add_subparsers(dest='core_action', required=True)
    e_get = ev_sub.add_parser('get')
    e_get.add_argument('event_id', type=int)
    e_ack = ev_sub.add_parser('ack')
    e_ack.add_argument('event_ids', type=int, nargs='+')
    e_ack.add_argument('--reason', default=None)

    # delivery
    dl = sub.add_parser('delivery', help='Delivery & judge verdict operations')
    dl_sub = dl.add_subparsers(dest='core_action', required=True)
    d_sub = dl_sub.add_parser('submit')
    d_sub.add_argument('request_id')
    d_sub.add_argument('--json', dest='json_payload', default=None)
    d_vrd = dl_sub.add_parser('verdict')
    d_vrd.add_argument('--json', dest='json_payload', default=None)

    # notification
    nf = sub.add_parser('notification', help='Notification operations')
    nf_sub = nf.add_subparsers(dest='core_action', required=True)
    nf_list = nf_sub.add_parser('list')
    nf_list.add_argument('--request-id', default=None)
    nf_list.add_argument('--open-only', action='store_true')
    nf_resp = nf_sub.add_parser('respond')
    nf_resp.add_argument('notification_id')
    nf_resp.add_argument('--answer', required=True)

    # signal
    sg = sub.add_parser('signal', help='Send an informational signal to Root')
    sg.add_argument('text')

    # visualize
    vz = sub.add_parser('visualize', help='Render text tree or HTML snapshot')
    vz.add_argument('--request-id', default=None)
    vz.add_argument('--output-path', default=None)

    # memory
    mem = sub.add_parser('memory', help='Cross-backend Markdown memory operations')
    mem_sub = mem.add_subparsers(dest='core_action', required=True)
    m_idx = mem_sub.add_parser('index')
    m_idx.add_argument('--role', default='human')
    m_idx.add_argument('--scope', default=None)
    m_idx.add_argument('--category', default=None)

    m_rd = mem_sub.add_parser('read')
    m_rd.add_argument('path')
    m_rd.add_argument('--role', default='human')

    m_sr = mem_sub.add_parser('search')
    m_sr.add_argument('query')
    m_sr.add_argument('--role', default='human')
    m_sr.add_argument('--limit', type=int, default=10)

    m_wr = mem_sub.add_parser('write')
    m_wr.add_argument('path')
    m_wr.add_argument('--role', default='human')
    m_wr.add_argument('--content', default=None)
    m_wr.add_argument('--json', dest='json_payload', default=None)
    m_wr.add_argument('--expected-hash', default=None)
    m_wr.add_argument('--scope', default=None)
    m_wr.add_argument('--summary', default=None)
    m_wr.add_argument('--verified-at', default=None)
    m_wr.add_argument('--owner', default=None)

    m_pr = mem_sub.add_parser('prompt')
    m_pr.add_argument('--role', default='goal')
    m_pr.add_argument('--hostname', default=None)


def handle_core(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config, store_path_override=args.db)
        core = AncorCore(cfg)
        actor = args.actor
        cref = args.confirmation_ref
        domain = args.core_domain

        if domain == 'overview':
            return _emit(core.overview(include_events=not args.no_events))

        if domain == 'signal':
            return _emit(core.signal_send(args.text, actor=actor, confirmation_ref=cref))

        if domain == 'visualize':
            return _emit(core.visualize(request_id=args.request_id, output_path=args.output_path))

        action = args.core_action
        if domain == 'request':
            if action == 'list':
                return _emit(core.request_list(include_closed=args.include_closed))
            if action == 'get':
                return _emit(core.request_get(
                    args.request_id,
                    include_content=args.include_content,
                    include_history=args.include_history,
                ))
            if action == 'read':
                return _emit(core.request_read(args.request_id, goal_id=args.goal_id))
            if action == 'create':
                payload = _read_json_arg(args.json_payload)
                title = payload.get('title') or args.title
                fields = payload.get('fields')
                if fields is None:
                    fields = {}
                    for k in ('outcome', 'constraint', 'advice', 'context'):
                        vals = getattr(args, k)
                        if vals:
                            fields[k] = list(vals)
                if not title or not fields:
                    raise ValueError('request create requires title and fields (via --json or flags)')
                return _emit(core.request_create(
                    title=title,
                    fields=fields,
                    input_text=payload.get('input'),
                    actor=actor,
                    confirmation_ref=cref,
                ))
            if action == 'revise':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.request_revise(
                    args.request_id,
                    operations=payload['operations'],
                    input_text=payload.get('input', 'revise'),
                    base=payload.get('base'),
                    actor=actor,
                    confirmation_ref=cref,
                ))
            if action == 'pause':
                return _emit(core.request_pause(args.request_id, reason=args.reason, actor=actor, confirmation_ref=cref))
            if action == 'resume':
                return _emit(core.request_resume(args.request_id, reason=args.reason, actor=actor, confirmation_ref=cref))
            if action == 'cancel':
                return _emit(core.request_cancel(args.request_id, reason=args.reason, actor=actor, confirmation_ref=cref))
            if action == 'accept':
                return _emit(core.request_accept(args.request_id, actor=actor, confirmation_ref=cref))
            if action == 'request-changes':
                return _emit(core.request_changes(args.request_id, feedback=args.feedback, actor=actor, confirmation_ref=cref))
            if action == 'advance':
                return _emit(core.request_advance(args.request_id))

        if domain == 'change':
            if action == 'propose':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.change_propose(
                    request_id=payload.get('request_id'),
                    operations=payload['operations'],
                    input_text=payload['input'],
                    base=payload.get('base'),
                    actor=actor,
                ))
            if action == 'commit':
                return _emit(core.change_commit(args.change_id, actor=actor, confirmation_ref=cref))
            if action == 'get':
                return _emit(core.change_get(args.change_id))
            if action == 'history':
                return _emit(core.change_history(args.request_id, limit=args.limit))

        if domain == 'goal':
            if action == 'list':
                return _emit(core.goal_list(
                    request_id=args.request_id,
                    parent_goal_id=args.parent_goal_id,
                    status=args.status,
                    include_terminal=args.include_terminal,
                ))
            if action == 'get':
                return _emit(core.goal_get(args.goal_id))
            if action == 'spawn':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.goal_spawn(payload, caller_goal_id=args.caller))
            if action == 'signal':
                return _emit(core.goal_signal(
                    args.goal_id, args.kind, message=args.message,
                    generation=args.generation, caller_goal_id=args.caller,
                ))
            if action == 'pause':
                return _emit(core.goal_pause(args.goal_id, mode=args.mode, caller_goal_id=args.caller))
            if action == 'resume':
                return _emit(core.goal_resume(args.goal_id, caller_goal_id=args.caller))
            if action == 'stop':
                return _emit(core.goal_stop(args.goal_id, reason=args.reason, caller_goal_id=args.caller))
            if action == 'wait':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.goal_wait(
                    args.goal_id,
                    wake_after_seconds=payload.get('wake_after_seconds', args.wake_after_seconds),
                    wake_at=payload.get('wake_at', args.wake_at),
                    watch_path=payload.get('watch_path', args.watch_path),
                    reason=payload.get('reason', args.reason),
                    handoff=payload.get('handoff'),
                    checklist=payload.get('checklist'),
                ))
            if action == 'handoff':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.goal_handoff(
                    args.goal_id,
                    handoff=payload['handoff'],
                    checklist=payload.get('checklist'),
                ))
            if action == 'shifts':
                return _emit(core.goal_shifts(args.goal_id))
            if action == 'report':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.goal_report(args.goal_id, payload))

        if domain == 'resource':
            if action == 'status':
                return _emit(core.cluster_status())
            if action == 'acquire':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.resource_acquire(
                    args.goal_id,
                    usage=payload.get('usage'),
                    tags=payload.get('tags'),
                    handle_id=payload.get('handle_id'),
                    command=payload.get('command'),
                    metadata=payload.get('metadata'),
                ))
            if action == 'release':
                return _emit(core.resource_release(
                    args.goal_id, handle_id=args.handle_id, status=args.status, detail=args.detail))

        if domain == 'question':
            if action == 'list':
                return _emit(core.question_list(request_id=args.request_id, include_closed=args.include_closed))
            if action == 'get':
                return _emit(core.question_get(args.question_id))
            if action == 'create':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.question_create(
                    request_id=payload['request_id'],
                    question_id=payload['id'],
                    body=payload['body'],
                    reason=payload['reason'],
                    point_ids=payload.get('point_ids') or [],
                    actor=actor,
                ))
            if action == 'answer':
                return _emit(core.question_answer(
                    args.question_id, body=args.body, actor=actor, confirmation_ref=cref))
            if action == 'withdraw':
                return _emit(core.question_withdraw(args.question_id, reason=args.reason, actor=actor))

        if domain == 'note':
            if action == 'list':
                return _emit(core.note_list(args.request_id))
            if action == 'add':
                return _emit(core.note_add(args.request_id, args.body, actor=actor))

        if domain == 'event':
            if action == 'get':
                return _emit(core.event_get(args.event_id))
            if action == 'ack':
                return _emit(core.event_ack(args.event_ids, reason=args.reason))

        if domain == 'delivery':
            if action == 'submit':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.delivery_submit(
                    args.request_id,
                    summary=payload['summary'],
                    artifacts=payload['artifacts'],
                    eval_evidence=payload.get('eval_evidence'),
                ))
            if action == 'verdict':
                payload = _read_json_arg(args.json_payload)
                return _emit(core.judge_verdict(
                    request_id=payload['request_id'],
                    delivery_event_id=payload.get('delivery_event_id'),
                    generation=payload['generation'],
                    decision=payload['decision'],
                    summary=payload['summary'],
                    evidence=payload.get('evidence'),
                    feedback=payload.get('feedback'),
                    artifacts=payload.get('artifacts'),
                ))

        if domain == 'notification':
            if action == 'list':
                return _emit(core.notification_list(request_id=args.request_id, open_only=args.open_only))
            if action == 'respond':
                return _emit(core.notification_respond(
                    args.notification_id, answer=args.answer, actor=actor, confirmation_ref=cref))

        if domain == 'memory':
            if action == 'index':
                return _emit(core.memory_index(role=args.role, scope=args.scope, category=args.category))
            if action == 'read':
                return _emit(core.memory_read(args.path, role=args.role))
            if action == 'search':
                return _emit(core.memory_search(args.query, role=args.role, limit=args.limit))
            if action == 'write':
                payload = _read_json_arg(args.json_payload)
                content = payload.get('content') or args.content
                if not content:
                    raise ValueError('memory write requires --content or --json with content')
                return _emit(core.memory_write(
                    args.path,
                    content,
                    role=args.role,
                    expected_hash=payload.get('expected_hash') or args.expected_hash,
                    scope=payload.get('scope') or args.scope,
                    summary=payload.get('summary') or args.summary,
                    verified_at=payload.get('verified_at') or args.verified_at,
                    owner=payload.get('owner') or args.owner,
                ))
            if action == 'prompt':
                return _emit({'prompt': core.memory_prompt_context(role=args.role, hostname=args.hostname)})

        raise ValueError(f'unhandled core command: {domain} {action}')
    except Exception as exc:
        return _emit_error(exc)
