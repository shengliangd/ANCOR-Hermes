"""Deterministic read-only visualizer over requirements.db (?mode=ro).

Generates either a self-contained interactive HTML/SVG Goal Tree & Request
dashboard or a compact Markdown tree summary directly from the authoritative
SQLite database — zero writes, zero LLM calls.
"""
from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
import sqlite3


def _ro_connect(store_path: str | Path) -> sqlite3.Connection:
    path = Path(store_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f'ANCOR store not found: {path}')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_snapshot(store_path: str | Path, *, request_id: str | None = None) -> dict:
    """Read authoritative requests, active_goals, judge_verdicts, and questions read-only."""
    conn = _ro_connect(store_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if request_id:
            reqs = [dict(r) for r in conn.execute('SELECT * FROM requests WHERE id=?', (request_id,))]
        else:
            reqs = [dict(r) for r in conn.execute('SELECT * FROM requests ORDER BY rowid DESC')]

        goals = []
        if 'active_goals' in tables:
            sql = 'SELECT * FROM active_goals' + (' WHERE request_id=?' if request_id else '') + ' ORDER BY created_at, goal_id'
            for r in conn.execute(sql, (request_id,) if request_id else ()):
                g = dict(r)
                g.setdefault('observed_models', '[]')
                for col in ('scope', 'budget', 'external_job_ids', 'pending_signals', 'observed_models'):
                    g[col] = json.loads(g[col]) if isinstance(g.get(col), str) else (g.get(col) or {})
                g['result'] = json.loads(g['result_json']) if g.get('result_json') else None
                g.pop('result_json', None)
                g.pop('session_id', None)
                goals.append(g)

        verdicts = []
        if 'judge_verdicts' in tables:
            sql = 'SELECT * FROM judge_verdicts' + (' WHERE request_id=?' if request_id else '') + ' ORDER BY id DESC'
            for r in conn.execute(sql, (request_id,) if request_id else ()):
                v = dict(r)
                v['feedback'] = json.loads(v['feedback']) if isinstance(v.get('feedback'), str) else []
                v['artifacts'] = json.loads(v['artifacts']) if isinstance(v.get('artifacts'), str) else []
                v['runtime'] = json.loads(v['runtime']) if isinstance(v.get('runtime'), str) else []
                verdicts.append(v)

        questions = []
        if 'questions' in tables:
            sql = 'SELECT id, request_id, body, state, revision FROM questions' + (
                ' WHERE request_id=?' if request_id else '') + ' ORDER BY created_at DESC'
            questions = [dict(r) for r in conn.execute(sql, (request_id,) if request_id else ())]

        return {'requests': reqs, 'goals': goals, 'judge_verdicts': verdicts, 'questions': questions}
    finally:
        conn.close()


def _format_budget_chips(budget: dict) -> str:
    parts = []
    for k, v in (budget.get('limits') or {}).items():
        parts.append(f'{k}: {v}')
    for k, vals in (budget.get('allowed') or {}).items():
        if vals:
            shown = ', '.join(str(x) for x in vals[:2]) + ('…' if len(vals) > 2 else '')
            parts.append(f'{k}=[{shown}]')
    return ' · '.join(parts)


def model_route_notes(goal: dict) -> list[str]:
    """Category + actually-used models; >1 distinct models means Hermes fell back mid-run."""
    notes = []
    if goal.get('category'):
        notes.append(f"category={goal['category']}")
    models = [m.get('model') for m in (goal.get('observed_models') or []) if m.get('model')]
    if models:
        notes.append('model=' + ' → '.join(models))
        if len(set(models)) > 1:
            notes.append('fallback')
    return notes


def judge_shares_model(verdict: dict, goals: list[dict]) -> list[str]:
    """Soft independence check: models used by both the Judge and the producing goals."""
    judge_models = {m.get('model') for m in (verdict.get('runtime') or []) if m.get('model')}
    produced = {m.get('model') for g in goals for m in (g.get('observed_models') or []) if m.get('model')}
    return sorted(judge_models & produced)


def render_text_tree(store_path: str | Path, *, request_id: str | None = None) -> str:
    """Render a compact Markdown/ASCII tree directly from requirements.db."""
    snap = load_snapshot(store_path, request_id=request_id)
    if not snap['requests']:
        return 'No ANCOR requests found.'
    goals_by_req: dict[str, list[dict]] = {}
    for g in snap['goals']:
        goals_by_req.setdefault(g['request_id'], []).append(g)
    verdicts_by_req: dict[str, list[dict]] = {}
    for v in snap['judge_verdicts']:
        verdicts_by_req.setdefault(v['request_id'], []).append(v)
    questions_by_req: dict[str, list[dict]] = {}
    for q in snap['questions']:
        if q['state'] == 'open':
            questions_by_req.setdefault(q['request_id'], []).append(q)

    lines = []
    for req in snap['requests']:
        rid = req['id']
        lines.append(f"- [{req['status']}] {req['title']} ({rid[:8]}, gen={req.get('generation', 1)})")
        qs = questions_by_req.get(rid, [])
        if qs:
            lines.append(f"  - Open questions: {len(qs)}")
        req_goals = goals_by_req.get(rid, [])
        children: dict[str, list[dict]] = {}
        for g in req_goals:
            children.setdefault(g['parent_goal_id'], []).append(g)

        def walk(parent_id: str, depth: int):
            for node in children.get(parent_id, []):
                indent = '  ' * depth
                p_tag = f" (by {node['paused_by_ancestor']})" if node.get('paused_by_ancestor') else ''
                budget_txt = _format_budget_chips(node.get('budget') or {})
                route = ' '.join(model_route_notes(node))
                suffix = (f' | {budget_txt}' if budget_txt else '') + (f' | {route}' if route else '')
                lines.append(f"{indent}- [{node['status']}{p_tag}] {node['goal_id']}: {node['objective'][:80]}{suffix}")
                walk(node['goal_id'], depth + 1)

        walk('root', 1)
        vs = verdicts_by_req.get(rid, [])
        if vs:
            latest = vs[0]
            lines.append(f"  - Latest Judge: {latest['decision']} (gen={latest['generation']}) — {latest['summary'][:80]}")
            shared = judge_shares_model(latest, req_goals)
            if shared:
                lines.append(f"  - ⚠ Judge shares model with producer: {', '.join(shared)}")
    return '\n'.join(lines)


def render_html(store_path: str | Path, *, request_id: str | None = None) -> str:
    """Render a self-contained read-only HTML/SVG Goal Tree view from requirements.db."""
    snap = load_snapshot(store_path, request_id=request_id)
    goals_by_req: dict[str, list[dict]] = {}
    for g in snap['goals']:
        goals_by_req.setdefault(g['request_id'], []).append(g)
    verdicts_by_req: dict[str, list[dict]] = {}
    for v in snap['judge_verdicts']:
        verdicts_by_req.setdefault(v['request_id'], []).append(v)
    open_q = [q for q in snap['questions'] if q['state'] == 'open']

    def render_goal_node(node: dict, children_map: dict[str, list[dict]]) -> str:
        status = escape(str(node['status']))
        gid = escape(str(node['goal_id']))
        obj = escape(str(node['objective']))
        budget = node.get('budget') or {}
        chips = []
        for k, v in (budget.get('limits') or {}).items():
            chips.append(f'<span class="chip">{escape(str(k))}: {escape(str(v))}</span>')
        for k, vals in (budget.get('allowed') or {}).items():
            if vals:
                chips.append(f'<span class="chip chip-allow">{escape(str(k))}: {escape(", ".join(map(str, vals[:2])))}</span>')
        for note in model_route_notes(node):
            cls = 'chip chip-warn' if note == 'fallback' else 'chip chip-model'
            chips.append(f'<span class="{cls}">{escape(note)}</span>')
        if node.get('paused_by_ancestor'):
            chips.append(f'<span class="chip chip-warn">paused by {escape(str(node["paused_by_ancestor"]))}</span>')
        constraints = budget.get('constraints') or []
        c_html = ''
        if constraints:
            items = ''.join(f'<li>{escape(str(c))}</li>' for c in constraints)
            c_html = f'<ul class="constraints">{items}</ul>'
        handles = node.get('external_job_ids') or []
        h_html = ''
        if handles:
            h_items = ''.join(
                f'<span class="chip chip-job">{escape(str(h.get("handle_id", h.get("job_id", "?"))))} ({escape(str(h.get("status", "?")))})</span>'
                for h in handles
            )
            h_html = f'<div class="chips">{h_items}</div>'
        kids = children_map.get(node['goal_id'], [])
        kids_html = ''.join(render_goal_node(k, children_map) for k in kids)
        kids_wrap = f'<div class="children">{kids_html}</div>' if kids_html else ''
        return (
            f'<div class="goal-node status-{status}">'
            f'<div class="goal-head"><span class="gid">{gid}</span>'
            f'<span class="badge st-{status}">{status}</span></div>'
            f'<div class="goal-obj">{obj}</div>'
            f'<div class="chips">{"".join(chips)}</div>'
            f'{h_html}{c_html}{kids_wrap}</div>'
        )

    req_blocks = []
    for req in snap['requests']:
        rid = req['id']
        r_goals = goals_by_req.get(rid, [])
        children_map: dict[str, list[dict]] = {}
        for g in r_goals:
            children_map.setdefault(g['parent_goal_id'], []).append(g)
        roots = children_map.get('root', [])
        tree_html = (''.join(render_goal_node(r, children_map) for r in roots)
                     if roots else '<p class="muted">暂无活跃 Goal 节点</p>')
        v_list = verdicts_by_req.get(rid, [])
        v_html = ''
        if v_list:
            def v_line(v):
                models = ', '.join(m.get('model', '?') for m in (v.get('runtime') or []))
                shared = judge_shares_model(v, r_goals)
                extra = (f' <span class="chip chip-model">judge model: {escape(models)}</span>' if models else '')
                extra += (f' <span class="chip chip-warn">shares model with producer: {escape(", ".join(shared))}</span>'
                          if shared else '')
                return (f'<li><span class="badge st-{escape(v["decision"])}">{escape(v["decision"])}</span> '
                        f'gen {v["generation"]} — {escape(v["summary"])}{extra}</li>')
            v_items = ''.join(v_line(v) for v in v_list[:5])
            v_html = f'<div class="section"><h3>Judge Verdicts</h3><ul class="verdicts">{v_items}</ul></div>'
        req_blocks.append(
            f'<article class="req-card">'
            f'<header><div><h2>{escape(req["title"])}</h2>'
            f'<small class="muted">{escape(rid)} · gen {req.get("generation", 1)}</small></div>'
            f'<span class="badge st-{escape(req["status"])}">{escape(req["status"])}</span></header>'
            f'<div class="section"><h3>Goal Tree ({len(r_goals)})</h3>{tree_html}</div>'
            f'{v_html}</article>'
        )

    body = ''.join(req_blocks) if req_blocks else '<p class="muted">No ANCOR requests found.</p>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>ANCOR Read-Only View</title>
<style>
:root{{--bg:#0f141b;--card:#171f2b;--border:#293548;--fg:#e6edf7;--muted:#8b9bb4;--accent:#60a5fa;--ok:#34d399;--warn:#fbbf24;--err:#f87171}}
@media(prefers-color-scheme:light){{:root{{--bg:#f8fafc;--card:#ffffff;--border:#e2e8f0;--fg:#0f172a;--muted:#64748b;--accent:#2563eb;--ok:#059669;--warn:#d97706;--err:#dc2626}}}}
*{{box-sizing:border-box}}body{{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,sans-serif}}
.wrap{{max-width:1120px;margin:0 auto}}.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0 24px}}
.kpi{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px}}.kpi span{{display:block;font-size:12px;color:var(--muted)}}.kpi b{{font-size:24px}}
.req-card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:16px}}
.req-card header{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;border-bottom:1px solid var(--border);padding-bottom:10px}}
.req-card h2{{margin:0;font-size:18px}}.section h3{{font-size:13px;color:var(--muted);margin:14px 0 8px;text-transform:uppercase;letter-spacing:.04em}}
.muted{{color:var(--muted)}}.badge{{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;border:1px solid var(--border)}}
.st-active,.st-running,.st-accept,.st-completed,.st-accepted{{color:var(--ok);border-color:var(--ok)}}
.st-paused,.st-waiting_external,.st-awaiting_acceptance,.st-reject{{color:var(--warn);border-color:var(--warn)}}
.st-cancelled,.st-blocked,.st-infeasible,.st-escalate{{color:var(--err);border-color:var(--err)}}
.goal-node{{border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:8px;padding:10px 12px;margin:8px 0;background:color-mix(in srgb,var(--bg) 55%,transparent)}}
.goal-head{{display:flex;justify-content:space-between;align-items:center;gap:8px}}.gid{{font-family:ui-monospace,monospace;font-size:12px;font-weight:600}}
.goal-obj{{margin:4px 0 6px}}.chips{{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}}
.chip{{font-size:11px;font-family:ui-monospace,monospace;padding:1px 7px;border-radius:6px;border:1px solid var(--border);color:var(--muted)}}
.chip-warn{{color:var(--warn);border-color:var(--warn)}}.chip-model{{color:var(--accent);border-style:dashed}}.chip-job{{color:var(--accent)}}
.constraints{{margin:6px 0 0 18px;padding:0;font-size:12px;color:var(--muted)}}.children{{margin-left:18px;padding-left:12px;border-left:1px dashed var(--border)}}
.verdicts{{margin:0;padding-left:18px;font-size:13px}}
</style></head><body><div class="wrap">
<h1 style="margin:0;font-size:22px">ANCOR 只读状态视图</h1>
<div class="kpis">
  <div class="kpi"><span>Requests</span><b>{len(snap["requests"])}</b></div>
  <div class="kpi"><span>Goals</span><b>{len(snap["goals"])}</b></div>
  <div class="kpi"><span>Open Questions</span><b>{len(open_q)}</b></div>
  <div class="kpi"><span>Judge Verdicts</span><b>{len(snap["judge_verdicts"])}</b></div>
</div>
{body}
</div></body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Generate read-only ANCOR Goal Tree visualization from requirements.db')
    parser.add_argument('--store', required=True, help='Path to requirements.db')
    parser.add_argument('--request-id', help='Optional request ID filter')
    parser.add_argument('--format', choices=['html', 'text'], default='html')
    parser.add_argument('--out', help='Optional output file path (defaults to stdout)')
    args = parser.parse_args(argv)
    rendered = (render_html(args.store, request_id=args.request_id)
                if args.format == 'html' else
                render_text_tree(args.store, request_id=args.request_id))
    if args.out:
        Path(args.out).write_text(rendered, encoding='utf-8')
    else:
        print(rendered)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
