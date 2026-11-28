"""Deterministic read-only visualizer over requirements.db (?mode=ro).

Generates either a self-contained interactive top-down Tree Diagram + Right Detail
Inspector HTML explorer or a compact Markdown tree summary directly from the
authoritative SQLite database — zero writes, zero LLM calls.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
    """Read authoritative requests, active_goals, judge_verdicts, questions, waits, and recent notes read-only."""
    conn = _ro_connect(store_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if request_id:
            reqs = [dict(r) for r in conn.execute('SELECT * FROM requests WHERE id=?', (request_id,))]
        else:
            reqs = [dict(r) for r in conn.execute('SELECT * FROM requests ORDER BY rowid DESC')]

        waits_by_goal: dict[str, dict] = {}
        if 'goal_waits' in tables:
            for r in conn.execute('SELECT goal_id, wake_at, watch_path, reason, created_at FROM goal_waits'):
                waits_by_goal[r['goal_id']] = dict(r)

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
                g['wait'] = waits_by_goal.get(g['goal_id'])
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
            cols = {r[1] for r in conn.execute('PRAGMA table_info(questions)')}
            select_cols = [c for c in ('id', 'request_id', 'body', 'reason', 'state', 'revision', 'created_at') if c in cols]
            order = 'ORDER BY created_at DESC' if 'created_at' in cols else 'ORDER BY rowid DESC'
            sql = f"SELECT {', '.join(select_cols)} FROM questions" + (
                ' WHERE request_id=?' if request_id else '') + f' {order}'
            questions = [dict(r) for r in conn.execute(sql, (request_id,) if request_id else ())]

        notes = []
        if 'request_notes' in tables:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(request_notes)')}
            where_parts = []
            params = []
            if request_id:
                where_parts.append('request_id=?')
                params.append(request_id)
            if 'deleted' in cols:
                where_parts.append('deleted=0')
            where = (' WHERE ' + ' AND '.join(where_parts)) if where_parts else ''
            select_cols = [c for c in ('id', 'request_id', 'body', 'actor', 'updated_at', 'created_at') if c in cols]
            order_col = 'updated_at' if 'updated_at' in cols else 'rowid'
            sql = f"SELECT {', '.join(select_cols)} FROM request_notes{where} ORDER BY {order_col} DESC LIMIT 120"
            notes = [dict(r) for r in conn.execute(sql, tuple(params))]

        return {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'requests': reqs,
            'goals': goals,
            'judge_verdicts': verdicts,
            'questions': questions,
            'notes': notes,
        }
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
    """Render a self-contained interactive Top-Down Tree Diagram + Right Detail Inspector HTML explorer."""
    snap = load_snapshot(store_path, request_id=request_id)
    goals_by_req: dict[str, list[dict]] = {}
    for g in snap['goals']:
        goals_by_req.setdefault(g['request_id'], []).append(g)
    for v in snap['judge_verdicts']:
        v['shared_models'] = judge_shares_model(v, goals_by_req.get(v['request_id'], []))

    payload_json = json.dumps(snap, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    return _HTML_TEMPLATE.replace('__ANCOR_DATA_JSON__', payload_json)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ANCOR Tree Explorer</title>
<style>
:root {
  --bg: #0b0f16;
  --canvas-bg: #0d121b;
  --dot: #1f2b3d;
  --surface: #131a25;
  --surface-2: #192231;
  --hover: #1f2b3e;
  --selected: #1b2f4d;
  --selected-ring: #3b82f6;
  --border: #263449;
  --edge: #354761;
  --fg: #e7eef8;
  --fg-soft: #b8c7dc;
  --muted: #7f92ad;
  --accent: #60a5fa;
  --ok: #34d399;
  --ok-bg: rgba(52, 211, 153, 0.14);
  --warn: #fbbf24;
  --warn-bg: rgba(251, 191, 36, 0.14);
  --err: #f87171;
  --err-bg: rgba(248, 113, 113, 0.14);
  --info: #38bdf8;
  --info-bg: rgba(56, 189, 248, 0.14);
  --muted-bg: rgba(127, 146, 173, 0.14);
  --shadow: 0 6px 20px rgba(0, 0, 0, 0.36);
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  --sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Noto Sans SC", sans-serif;
}
[data-theme="light"] {
  --bg: #f4f7fb;
  --canvas-bg: #f1f5f9;
  --dot: #cbd5e1;
  --surface: #ffffff;
  --surface-2: #f8fafc;
  --hover: #f1f5f9;
  --selected: #eff6ff;
  --selected-ring: #2563eb;
  --border: #dbe3ee;
  --edge: #94a3b8;
  --fg: #0f172a;
  --fg-soft: #334155;
  --muted: #64748b;
  --accent: #2563eb;
  --ok: #059669;
  --ok-bg: rgba(5, 150, 105, 0.10);
  --warn: #d97706;
  --warn-bg: rgba(217, 119, 6, 0.12);
  --err: #dc2626;
  --err-bg: rgba(220, 38, 38, 0.10);
  --info: #0284c7;
  --info-bg: rgba(2, 132, 199, 0.10);
  --muted-bg: rgba(100, 116, 139, 0.10);
  --shadow: 0 4px 14px rgba(15, 23, 42, 0.08);
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; overflow: hidden; background: var(--bg); color: var(--fg); font: 13px/1.5 var(--sans); }
button, input { font: inherit; color: inherit; }

/* Top bar */
.topbar {
  height: 50px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  user-select: none;
}
.brand { font-weight: 700; font-size: 14px; letter-spacing: 0.2px; display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.brand-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.stats { display: flex; gap: 6px; align-items: center; flex-wrap: nowrap; overflow: hidden; }
.stat-pill {
  padding: 2px 8px; border-radius: 999px; font-size: 11.5px;
  background: var(--surface-2); border: 1px solid var(--border); color: var(--fg-soft); white-space: nowrap;
}
.stat-pill.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 35%, var(--border)); background: var(--ok-bg); }
.stat-pill.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, var(--border)); background: var(--warn-bg); }
.stat-pill.info { color: var(--info); border-color: color-mix(in srgb, var(--info) 35%, var(--border)); background: var(--info-bg); }
.spacer { flex: 1; }
.search {
  width: 210px; padding: 5px 10px; border-radius: 7px;
  border: 1px solid var(--border); background: var(--surface-2); color: var(--fg); outline: none; font-size: 12.5px;
}
.search:focus { border-color: var(--accent); }
.seg {
  display: inline-flex; background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 7px; padding: 2px; gap: 2px;
}
.seg button {
  border: 0; background: transparent; padding: 3px 9px; border-radius: 5px;
  cursor: pointer; font-size: 12px; color: var(--muted); white-space: nowrap;
}
.seg button.active { background: var(--selected); color: var(--accent); font-weight: 600; }
.icon-btn {
  border: 1px solid var(--border); background: var(--surface-2); color: var(--fg-soft);
  padding: 4px 9px; border-radius: 7px; cursor: pointer; font-size: 12px; white-space: nowrap;
}
.icon-btn:hover { background: var(--hover); color: var(--fg); }
.icon-btn.active { border-color: var(--accent); color: var(--accent); background: var(--selected); }

/* Split Workspace */
.workspace { display: flex; height: calc(100% - 50px); width: 100%; }
.left-pane {
  width: 64%; min-width: 340px; max-width: 80%;
  display: flex; flex-direction: column;
  background: var(--canvas-bg); border-right: 1px solid var(--border);
  position: relative; overflow: hidden;
}
.resizer { width: 5px; cursor: col-resize; background: transparent; flex-shrink: 0; margin-left: -2px; z-index: 15; }
.resizer:hover, .resizer.dragging { background: var(--accent); opacity: 0.55; }
.right-pane { flex: 1; min-width: 320px; overflow-y: auto; background: var(--bg); padding: 20px 22px 48px; }

/* Request switcher strip */
.req-strip {
  display: flex; align-items: center; gap: 6px; padding: 7px 12px;
  background: var(--surface); border-bottom: 1px solid var(--border);
  overflow-x: auto; flex-shrink: 0; user-select: none;
}
.req-strip::-webkit-scrollbar { height: 5px; }
.req-strip::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; }
.req-pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--fg-soft); cursor: pointer;
  font-size: 12px; white-space: nowrap; max-width: 240px;
}
.req-pill:hover { background: var(--hover); color: var(--fg); }
.req-pill.active { background: var(--selected); border-color: var(--selected-ring); color: var(--fg); font-weight: 600; }
.req-pill .pill-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.req-pill .pill-count { font-family: var(--mono); font-size: 10.5px; color: var(--muted); }

/* Interactive Tree Canvas */
.canvas-viewport {
  flex: 1; position: relative; overflow: hidden; cursor: grab; user-select: none;
  background-color: var(--canvas-bg);
  background-image: radial-gradient(var(--dot) 1.2px, transparent 1.2px);
  background-size: 22px 22px;
}
.canvas-viewport.panning { cursor: grabbing; }
.canvas-stage {
  position: absolute; top: 0; left: 0; transform-origin: 0 0;
  will-change: transform;
}
.edges-svg {
  position: absolute; top: 0; left: 0; overflow: visible; pointer-events: none;
}
.edge-path {
  fill: none; stroke: var(--edge); stroke-width: 1.8;
  transition: stroke 0.15s ease, stroke-width 0.15s ease, opacity 0.15s ease;
}
.edge-path.running { stroke: color-mix(in srgb, var(--accent) 75%, var(--edge)); }
.edge-path.waiting_external { stroke: var(--info); stroke-dasharray: 5 4; }
.edge-path.blocked, .edge-path.infeasible { stroke: color-mix(in srgb, var(--err) 75%, var(--edge)); }
.edge-path.question { stroke: var(--warn); stroke-dasharray: 4 4; }
.edge-path.completed, .edge-path.cancelled { opacity: 0.65; }
.edge-path.highlight { stroke: var(--accent); stroke-width: 2.6; opacity: 1; }

/* Tree Node Cards */
.node-card {
  position: absolute;
  border-radius: 11px;
  background: var(--surface);
  border: 1.5px solid var(--border);
  box-shadow: var(--shadow);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  overflow: visible;
  transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.12s ease;
}
.node-card:hover {
  border-color: color-mix(in srgb, var(--accent) 65%, var(--border));
  transform: translateY(-1px);
  z-index: 4;
}
.node-card.selected {
  border-color: var(--selected-ring);
  background: var(--selected);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--selected-ring) 28%, transparent), var(--shadow);
  z-index: 6;
}
.node-card.dimmed { opacity: 0.58; }
.node-card.dimmed:hover, .node-card.dimmed.selected { opacity: 1; }

.node-accent {
  height: 4px; width: 100%; border-radius: 9px 9px 0 0; flex-shrink: 0;
  background: var(--muted);
}
.node-accent.st-active, .node-accent.st-completed, .node-accent.st-accepted { background: var(--ok); }
.node-accent.st-running { background: var(--accent); }
.node-accent.st-waiting_external { background: var(--info); }
.node-accent.st-awaiting_acceptance, .node-accent.st-paused, .node-accent.st-question { background: var(--warn); }
.node-accent.st-blocked, .node-accent.st-infeasible { background: var(--err); }

.node-body {
  flex: 1; padding: 8px 11px 9px; display: flex; flex-direction: column; gap: 5px; min-height: 0;
}
.node-head {
  display: flex; align-items: center; gap: 6px; min-width: 0;
}
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--muted); }
.dot.st-active, .dot.st-completed, .dot.st-accepted { background: var(--ok); }
.dot.st-running { background: var(--accent); box-shadow: 0 0 7px var(--accent); }
.dot.st-waiting_external { background: var(--info); }
.dot.st-awaiting_acceptance, .dot.st-paused, .dot.st-question { background: var(--warn); }
.dot.st-blocked, .dot.st-infeasible { background: var(--err); }

.node-id {
  font-family: var(--mono); font-size: 11px; color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
}
.node-status {
  font-family: var(--mono); font-size: 10px; font-weight: 600;
  padding: 1px 6px; border-radius: 999px; white-space: nowrap; flex-shrink: 0;
  background: var(--muted-bg); color: var(--muted);
}
.node-status.st-active, .node-status.st-completed, .node-status.st-accepted { background: var(--ok-bg); color: var(--ok); }
.node-status.st-running { background: var(--info-bg); color: var(--accent); }
.node-status.st-waiting_external { background: var(--info-bg); color: var(--info); }
.node-status.st-awaiting_acceptance, .node-status.st-paused, .node-status.st-question { background: var(--warn-bg); color: var(--warn); }
.node-status.st-blocked, .node-status.st-infeasible { background: var(--err-bg); color: var(--err); }

.node-title {
  font-size: 12.5px; font-weight: 600; color: var(--fg); line-height: 1.38;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  word-break: break-word;
}
.node-card.kind-req .node-title { font-size: 13px; }
.node-card.kind-question {
  border-color: color-mix(in srgb, var(--warn) 48%, var(--border));
  background: color-mix(in srgb, var(--warn) 6%, var(--surface));
}
.node-card.kind-question.selected {
  background: color-mix(in srgb, var(--warn) 12%, var(--selected));
}

.node-foot {
  margin-top: auto; display: flex; align-items: center; gap: 5px; flex-wrap: nowrap; overflow: hidden;
}
.mini-chip {
  font-family: var(--mono); font-size: 10px; padding: 1px 6px; border-radius: 5px;
  background: var(--surface-2); border: 1px solid var(--border); color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 112px;
}
.mini-chip.ok { color: var(--ok); background: var(--ok-bg); border-color: transparent; }
.mini-chip.info { color: var(--info); background: var(--info-bg); border-color: transparent; }
.mini-chip.warn { color: var(--warn); background: var(--warn-bg); border-color: transparent; }
.mini-chip.err { color: var(--err); background: var(--err-bg); border-color: transparent; }

/* Expand / Collapse Handle at Bottom Center of Node */
.node-toggle {
  position: absolute;
  left: 50%; bottom: -12px; transform: translateX(-50%);
  min-width: 24px; height: 22px; padding: 0 7px;
  border-radius: 999px;
  border: 1.5px solid var(--border);
  background: var(--surface);
  color: var(--fg-soft);
  font-family: var(--mono); font-size: 11px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center;
  cursor: pointer; z-index: 8;
  box-shadow: 0 2px 8px rgba(0,0,0,0.28);
}
.node-toggle:hover {
  border-color: var(--accent); color: var(--accent); background: var(--selected);
}
.node-toggle.collapsed {
  border-color: var(--accent); color: var(--accent); background: var(--selected);
}

/* Floating Canvas Controls */
.canvas-controls {
  position: absolute; left: 14px; bottom: 14px; z-index: 12;
  display: flex; align-items: center; gap: 5px;
  padding: 5px 7px; border-radius: 10px;
  background: color-mix(in srgb, var(--surface) 92%, transparent);
  border: 1px solid var(--border); box-shadow: var(--shadow);
  backdrop-filter: blur(8px); user-select: none;
}
.ctrl-btn {
  border: 0; background: transparent; color: var(--fg-soft);
  padding: 4px 8px; border-radius: 6px; cursor: pointer; font-size: 12px;
}
.ctrl-btn:hover { background: var(--hover); color: var(--fg); }
.ctrl-sep { width: 1px; height: 16px; background: var(--border); margin: 0 2px; }
.zoom-readout { font-family: var(--mono); font-size: 11.5px; color: var(--muted); min-width: 42px; text-align: center; }

.canvas-hint {
  position: absolute; right: 14px; bottom: 14px; z-index: 11;
  padding: 4px 10px; border-radius: 8px; font-size: 11px; color: var(--muted);
  background: color-mix(in srgb, var(--surface) 84%, transparent);
  border: 1px solid var(--border); pointer-events: none;
}

.empty-canvas {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; color: var(--muted); gap: 8px; font-size: 14px;
}

/* Detail Inspector (Right Pane) */
.empty-detail { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--muted); gap: 8px; }
.breadcrumbs { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 12px; color: var(--muted); margin-bottom: 10px; }
.crumb { cursor: pointer; color: var(--accent); }
.crumb:hover { text-decoration: underline; }
.detail-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.detail-title { font-size: 17px; font-weight: 700; margin: 0 0 4px; line-height: 1.35; word-break: break-word; }
.detail-sub { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.badge {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
  font-family: var(--mono); white-space: nowrap;
  background: var(--muted-bg); color: var(--muted);
}
.badge.st-active, .badge.st-completed, .badge.st-accepted { background: var(--ok-bg); color: var(--ok); }
.badge.st-running { background: var(--info-bg); color: var(--accent); }
.badge.st-waiting_external { background: var(--info-bg); color: var(--info); }
.badge.st-awaiting_acceptance, .badge.st-paused, .badge.st-question { background: var(--warn-bg); color: var(--warn); }
.badge.st-blocked, .badge.st-infeasible { background: var(--err-bg); color: var(--err); }

.detail-actions { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }

.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 12px;
}
.card.warn-card { border-color: color-mix(in srgb, var(--warn) 50%, var(--border)); background: color-mix(in srgb, var(--warn) 5%, var(--surface)); }
.card.err-card { border-color: color-mix(in srgb, var(--err) 50%, var(--border)); background: color-mix(in srgb, var(--err) 5%, var(--surface)); }
.card.info-card { border-color: color-mix(in srgb, var(--info) 50%, var(--border)); background: color-mix(in srgb, var(--info) 5%, var(--surface)); }
.card-title {
  font-size: 11.5px; font-weight: 700; letter-spacing: 0.4px; text-transform: uppercase;
  color: var(--muted); margin: 0 0 10px; display: flex; align-items: center; justify-content: space-between;
}
.kv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(155px, 1fr)); gap: 9px; }
.kv { background: var(--surface-2); border: 1px solid var(--border); border-radius: 7px; padding: 7px 10px; }
.kv-k { font-size: 11px; color: var(--muted); margin-bottom: 2px; }
.kv-v { font-family: var(--mono); font-size: 12px; color: var(--fg); word-break: break-all; }

.prose { white-space: pre-wrap; word-break: break-word; color: var(--fg-soft); font-size: 13px; line-height: 1.6; margin: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  font-family: var(--mono); font-size: 11px; padding: 2px 8px; border-radius: 6px;
  background: var(--surface-2); border: 1px solid var(--border); color: var(--fg-soft);
}
.chip.info { color: var(--info); border-color: color-mix(in srgb, var(--info) 40%, var(--border)); }
.chip.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, var(--border)); }
.chip.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, var(--border)); }
.chip.err { color: var(--err); border-color: color-mix(in srgb, var(--err) 40%, var(--border)); }

.list { margin: 0; padding-left: 18px; color: var(--fg-soft); }
.list li { margin: 4px 0; word-break: break-word; }

table.tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
table.tbl th, table.tbl td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
table.tbl th { color: var(--muted); font-weight: 600; font-size: 11px; }
table.tbl td.mono { font-family: var(--mono); font-size: 11.5px; word-break: break-all; }
table.tbl tr:last-child td { border-bottom: 0; }

details.fold { border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; margin-bottom: 8px; background: var(--surface-2); }
details.fold summary { cursor: pointer; font-weight: 600; font-size: 12px; color: var(--fg-soft); }
details.fold[open] summary { margin-bottom: 8px; }
.child-link {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 7px 10px; border-radius: 7px; border: 1px solid var(--border);
  background: var(--surface-2); margin-bottom: 6px; cursor: pointer;
}
.child-link:hover { border-color: var(--accent); background: var(--hover); }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand"><span class="brand-dot"></span>ANCOR Tree</div>
  <div class="stats" id="topStats"></div>
  <div class="spacer"></div>
  <input id="searchBox" class="search" type="search" placeholder="搜索需求 / Goal / 摘要…">
  <div class="seg" id="scopeFilter">
    <button data-scope="open" class="active">未关闭</button>
    <button data-scope="attention">需介入</button>
    <button data-scope="all">全部</button>
  </div>
  <button id="toggleTerminalBtn" class="icon-btn" title="隐藏/显示已终态 Goal">隐藏终态 Goal</button>
  <button id="themeBtn" class="icon-btn" title="切换明暗主题">☀/☾</button>
</header>

<div class="workspace">
  <section class="left-pane" id="leftPane">
    <div class="req-strip" id="reqStrip"></div>
    <div class="canvas-viewport" id="treeContainer">
      <div class="canvas-stage" id="canvasStage">
        <svg class="edges-svg" id="edgesSvg"></svg>
        <div id="nodesLayer"></div>
      </div>
      <div class="empty-canvas" id="emptyCanvas" style="display:none">没有匹配的需求或 Goal</div>
      <div class="canvas-controls">
        <button class="ctrl-btn" id="zoomOutBtn" title="缩小">−</button>
        <span class="zoom-readout" id="zoomReadout">100%</span>
        <button class="ctrl-btn" id="zoomInBtn" title="放大">+</button>
        <span class="ctrl-sep"></span>
        <button class="ctrl-btn" id="fitBtn" title="适应画布">适应画布</button>
        <button class="ctrl-btn" id="expandAllBtn">全部展开</button>
        <button class="ctrl-btn" id="collapseAllBtn">全部折叠</button>
      </div>
      <div class="canvas-hint">拖拽画布平移 · 滚轮缩放 · 点击节点看右侧详情 · 点击底部的 +N/− 展开/折叠子树</div>
    </div>
  </section>
  <div class="resizer" id="resizer"></div>
  <main class="right-pane" id="detailPane"></main>
</div>

<script id="ancor-data" type="application/json">__ANCOR_DATA_JSON__</script>
<script>
(function() {
  const DATA = JSON.parse(document.getElementById('ancor-data').textContent);
  const TERMINAL_REQ = new Set(['accepted', 'cancelled', 'superseded', 'archived']);
  const TERMINAL_GOAL = new Set(['completed', 'blocked', 'infeasible', 'cancelled']);
  const ACTIVE_HANDLE = new Set(['pending', 'running', 'queued', 'active']);

  // Layout geometry constants
  const SIZE = {
    req:      { w: 272, h: 108 },
    goal:     { w: 244, h: 104 },
    question: { w: 224, h: 86  },
  };
  const H_GAP = 26;
  const V_GAP = 64;
  const TREE_GAP_X = 76;
  const SHELF_GAP_Y = 92;
  const MAX_SHELF_WIDTH = 2200;
  const PAD = 56;

  // Indexing
  const reqById = new Map();
  const goalById = new Map();
  const goalsByReq = new Map();
  const childrenByParent = new Map();
  const questionsByReq = new Map();
  const verdictsByReq = new Map();
  const notesByReq = new Map();

  for (const r of DATA.requests || []) {
    reqById.set(r.id, r);
    goalsByReq.set(r.id, []);
    questionsByReq.set(r.id, []);
    verdictsByReq.set(r.id, []);
    notesByReq.set(r.id, []);
  }
  for (const g of DATA.goals || []) {
    goalById.set(g.goal_id, g);
    if (!goalsByReq.has(g.request_id)) goalsByReq.set(g.request_id, []);
    goalsByReq.get(g.request_id).push(g);
    const pkey = `${g.request_id}::${g.parent_goal_id || 'root'}`;
    if (!childrenByParent.has(pkey)) childrenByParent.set(pkey, []);
    childrenByParent.get(pkey).push(g);
  }
  for (const q of DATA.questions || []) {
    if (!questionsByReq.has(q.request_id)) questionsByReq.set(q.request_id, []);
    questionsByReq.get(q.request_id).push(q);
  }
  for (const v of DATA.judge_verdicts || []) {
    if (!verdictsByReq.has(v.request_id)) verdictsByReq.set(v.request_id, []);
    verdictsByReq.get(v.request_id).push(v);
  }
  for (const n of DATA.notes || []) {
    if (!notesByReq.has(n.request_id)) notesByReq.set(n.request_id, []);
    notesByReq.get(n.request_id).push(n);
  }

  const goalRank = s => (s === 'running' ? 0 : s === 'waiting_external' ? 1 : s === 'paused' ? 2 : s === 'blocked' ? 3 : 4);
  for (const arr of childrenByParent.values()) {
    arr.sort((a, b) => (goalRank(a.status) - goalRank(b.status)) || ((b.updated_at || 0) - (a.updated_at || 0)));
  }

  // State
  const state = {
    scope: (DATA.requests || []).some(r => !TERMINAL_REQ.has(r.status)) ? 'open' : 'all',
    focusedReqId: null, // null = show all matching requests in forest; string = focus single request tree
    hideTerminalGoals: false,
    query: '',
    expanded: new Set(),
    selectedKey: null,
    nodesByKey: new Map(),
    edges: [],
    bounds: { width: 800, height: 600 },
    tx: 0, ty: 0, scale: 1,
  };

  // Default expand active/open requests and their L1 running/waiting/blocked goals
  for (const r of DATA.requests || []) {
    if (!TERMINAL_REQ.has(r.status)) {
      state.expanded.add('req:' + r.id);
    }
  }
  for (const g of DATA.goals || []) {
    if (!TERMINAL_GOAL.has(g.status)) {
      state.expanded.add('goal:' + g.goal_id);
    }
  }

  // Theme init
  const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  document.documentElement.dataset.theme = prefersLight ? 'light' : 'dark';

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function fmtTime(ts) {
    if (!ts) return '—';
    const d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return String(ts);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function activeGpu(goal) {
    let sum = 0;
    for (const h of goal.external_job_ids || []) {
      if (ACTIVE_HANDLE.has(h.status || 'running') && h.usage && h.usage.gpu) sum += Number(h.usage.gpu);
    }
    return sum;
  }
  function shortModel(goal) {
    const ms = (goal.observed_models || []).map(m => m.model).filter(Boolean);
    if (!ms.length) return '';
    const last = ms[ms.length - 1];
    return last.split('/').pop();
  }

  // Top stats
  function renderTopStats() {
    const openReqs = (DATA.requests || []).filter(r => !TERMINAL_REQ.has(r.status));
    const attentionReqs = openReqs.filter(r =>
      r.status === 'awaiting_acceptance' ||
      (questionsByReq.get(r.id) || []).some(q => q.state === 'open')
    ).length;
    const runningGoals = (DATA.goals || []).filter(g => g.status === 'running' || g.status === 'waiting_external').length;
    let totalGpu = 0;
    for (const g of DATA.goals || []) totalGpu += activeGpu(g);
    document.getElementById('topStats').innerHTML = [
      `<span class="stat-pill ok">未关闭 ${openReqs.length}</span>`,
      attentionReqs ? `<span class="stat-pill warn">需介入 ${attentionReqs}</span>` : `<span class="stat-pill">需介入 0</span>`,
      `<span class="stat-pill info">运行/等待 Goal ${runningGoals}</span>`,
      `<span class="stat-pill">账面 GPU ${totalGpu}</span>`,
    ].join('');
  }

  function goalMatchesQuery(g, q) {
    if (!q) return true;
    const hay = `${g.goal_id} ${g.objective || ''} ${g.status} ${g.category || ''} ${(g.result && g.result.summary) || ''}`.toLowerCase();
    return hay.includes(q);
  }
  function reqMatchesQuery(r, q) {
    if (!q) return true;
    const hay = `${r.id} ${r.title || ''} ${r.status} ${r.brief || ''}`.toLowerCase();
    return hay.includes(q);
  }

  function getFilteredRequests() {
    const q = state.query.trim().toLowerCase();
    const reqOrder = s => (s === 'active' ? 0 : s === 'awaiting_acceptance' ? 1 : s === 'paused' ? 2 : s === 'accepted' ? 3 : 4);
    const reqs = [...(DATA.requests || [])].sort((a, b) => reqOrder(a.status) - reqOrder(b.status));
    const matched = [];
    for (const r of reqs) {
      const openQs = (questionsByReq.get(r.id) || []).filter(x => x.state === 'open');
      const needsAttention = !TERMINAL_REQ.has(r.status) && (openQs.length > 0 || r.status === 'awaiting_acceptance');
      if (state.scope === 'open' && TERMINAL_REQ.has(r.status)) continue;
      if (state.scope === 'attention' && !needsAttention) continue;
      const allReqGoals = goalsByReq.get(r.id) || [];
      if (q && !reqMatchesQuery(r, q) && !allReqGoals.some(g => goalMatchesQuery(g, q))) continue;
      matched.push(r);
    }
    return matched;
  }

  // Build hierarchical tree structure for a Request
  function countDescendants(node) {
    let total = 0;
    for (const c of node.allChildren) {
      total += 1 + countDescendants(c);
    }
    return total;
  }

  function buildReqTree(r, q) {
    const openQs = (questionsByReq.get(r.id) || []).filter(x => x.state === 'open');
    const goalFilter = g => !state.hideTerminalGoals || !TERMINAL_GOAL.has(g.status);

    function buildGoalNode(g, parentKey) {
      const key = 'goal:' + g.goal_id;
      const rawKids = (childrenByParent.get(`${r.id}::${g.goal_id}`) || []).filter(goalFilter);
      const allChildren = rawKids.map(c => buildGoalNode(c, key));
      const isExpanded = Boolean(q) || state.expanded.has(key);
      const node = {
        key, kind: 'goal', item: g, req: r, parentKey,
        w: SIZE.goal.w, h: SIZE.goal.h,
        allChildren,
        visibleChildren: isExpanded ? allChildren : [],
        isExpanded,
      };
      node.hiddenDescCount = isExpanded ? 0 : countDescendants(node);
      return node;
    }

    const reqKey = 'req:' + r.id;
    const qNodes = openQs.map(qItem => ({
      key: 'q:' + qItem.id, kind: 'question', item: qItem, req: r, parentKey: reqKey,
      w: SIZE.question.w, h: SIZE.question.h,
      allChildren: [], visibleChildren: [], isExpanded: false, hiddenDescCount: 0,
    }));
    const l1Goals = (childrenByParent.get(`${r.id}::root`) || []).filter(goalFilter).map(g => buildGoalNode(g, reqKey));
    const allChildren = [...qNodes, ...l1Goals];
    const isExpanded = Boolean(q) || state.expanded.has(reqKey);
    const root = {
      key: reqKey, kind: 'req', item: r, req: r, parentKey: null,
      w: SIZE.req.w, h: SIZE.req.h,
      allChildren,
      visibleChildren: isExpanded ? allChildren : [],
      isExpanded,
    };
    root.hiddenDescCount = isExpanded ? 0 : countDescendants(root);
    return root;
  }

  // Tidy top-down tree layout (bottom-up subtree width, top-down coordinates)
  function measureTree(node) {
    if (!node.visibleChildren.length) {
      node.subW = node.w;
      node.subH = node.h;
      return;
    }
    let sumW = 0;
    let maxChildH = 0;
    for (let i = 0; i < node.visibleChildren.length; i++) {
      const c = node.visibleChildren[i];
      measureTree(c);
      sumW += c.subW + (i > 0 ? H_GAP : 0);
      if (c.subH > maxChildH) maxChildH = c.subH;
    }
    node.subW = Math.max(node.w, sumW);
    node.subH = node.h + V_GAP + maxChildH;
  }

  function assignCoords(node, leftX, topY, outNodes, outEdges) {
    node.x = leftX + (node.subW - node.w) / 2;
    node.y = topY;
    outNodes.push(node);

    if (!node.visibleChildren.length) return;
    let totalKidsW = 0;
    for (let i = 0; i < node.visibleChildren.length; i++) {
      totalKidsW += node.visibleChildren[i].subW + (i > 0 ? H_GAP : 0);
    }
    let curX = leftX + (node.subW - totalKidsW) / 2;
    const childY = topY + node.h + V_GAP;
    for (const c of node.visibleChildren) {
      assignCoords(c, curX, childY, outNodes, outEdges);
      outEdges.push({ from: node, to: c });
      curX += c.subW + H_GAP;
    }
  }

  function renderReqStrip(matchedReqs) {
    const strip = document.getElementById('reqStrip');
    if (state.focusedReqId && !matchedReqs.some(r => r.id === state.focusedReqId)) {
      state.focusedReqId = null;
    }
    const pills = [
      `<button class="req-pill${state.focusedReqId === null ? ' active' : ''}" data-focus="">
        <span class="pill-label">全部树 (${matchedReqs.length})</span>
      </button>`
    ];
    for (const r of matchedReqs) {
      const gCount = (goalsByReq.get(r.id) || []).length;
      const openQ = (questionsByReq.get(r.id) || []).filter(q => q.state === 'open').length;
      const active = state.focusedReqId === r.id ? ' active' : '';
      pills.push(
        `<button class="req-pill${active}" data-focus="${esc(r.id)}" title="${esc(r.title || r.id)}">
          <span class="dot st-${esc(r.status)}"></span>
          <span class="pill-label">${esc(r.title || r.id)}</span>
          <span class="pill-count">${openQ ? '❓' + openQ + ' · ' : ''}${gCount}</span>
        </button>`
      );
    }
    strip.innerHTML = pills.join('');
    for (const btn of strip.querySelectorAll('.req-pill')) {
      btn.addEventListener('click', () => {
        const fid = btn.dataset.focus || null;
        state.focusedReqId = fid;
        if (fid) {
          state.expanded.add('req:' + fid);
          state.selectedKey = 'req:' + fid;
        }
        renderGraph({ autoFit: true });
      });
    }
  }

  function ancestorKeysOf(key) {
    const set = new Set();
    let cur = state.nodesByKey.get(key);
    while (cur) {
      set.add(cur.key);
      cur = cur.parentKey ? state.nodesByKey.get(cur.parentKey) : null;
    }
    return set;
  }

  function renderNodeCardHtml(n, isSelected) {
    const sel = isSelected ? ' selected' : '';
    let toggleHtml = '';
    if (n.allChildren.length > 0) {
      const cls = n.isExpanded ? '' : ' collapsed';
      const txt = n.isExpanded ? '−' : ('+' + n.hiddenDescCount);
      toggleHtml = `<button class="node-toggle${cls}" data-toggle="${esc(n.key)}" title="${n.isExpanded ? '折叠子树' : '展开子树'}">${txt}</button>`;
    }

    if (n.kind === 'req') {
      const r = n.item;
      const allG = goalsByReq.get(r.id) || [];
      const runningG = allG.filter(g => g.status === 'running' || g.status === 'waiting_external').length;
      const openQs = (questionsByReq.get(r.id) || []).filter(q => q.state === 'open').length;
      const gpu = allG.reduce((a, g) => a + activeGpu(g), 0);
      const vs = verdictsByReq.get(r.id) || [];
      const dimmed = TERMINAL_REQ.has(r.status) ? ' dimmed' : '';
      const foot = [];
      foot.push(`<span class="mini-chip${runningG ? ' info' : ''}">Goal ${allG.length}${runningG ? ` (${runningG}活跃)` : ''}</span>`);
      if (openQs) foot.push(`<span class="mini-chip warn">❓${openQs}</span>`);
      if (gpu) foot.push(`<span class="mini-chip info">GPU ${gpu}</span>`);
      if (vs.length) {
        const d = vs[0].decision;
        foot.push(`<span class="mini-chip ${d === 'pass' ? 'ok' : d === 'fail' ? 'err' : 'warn'}">J:${esc(d)}</span>`);
      }
      return `<div class="node-card kind-req${sel}${dimmed}" data-key="${esc(n.key)}" style="left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px">
        <div class="node-accent st-${esc(r.status)}"></div>
        <div class="node-body">
          <div class="node-head">
            <span class="dot st-${esc(r.status)}"></span>
            <span class="node-id">${esc(r.id.slice(0, 8))} · gen=${r.generation || 1}</span>
            <span class="node-status st-${esc(r.status)}">${esc(r.status)}</span>
          </div>
          <div class="node-title">${esc(r.title || '(无标题)')}</div>
          <div class="node-foot">${foot.join('')}</div>
        </div>
        ${toggleHtml}
      </div>`;
    }

    if (n.kind === 'question') {
      const q = n.item;
      return `<div class="node-card kind-question${sel}" data-key="${esc(n.key)}" style="left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px">
        <div class="node-accent st-question"></div>
        <div class="node-body">
          <div class="node-head">
            <span class="dot st-question"></span>
            <span class="node-id">❓ 待回答 · ${esc(q.id.slice(0, 8))}</span>
            <span class="node-status st-question">open</span>
          </div>
          <div class="node-title">${esc(q.body || '')}</div>
        </div>
      </div>`;
    }

    // Goal node
    const g = n.item;
    const gpu = activeGpu(g);
    const model = shortModel(g);
    const dimmed = (g.status === 'completed' || g.status === 'cancelled') ? ' dimmed' : '';
    const foot = [];
    if (g.category) foot.push(`<span class="mini-chip">${esc(g.category)}</span>`);
    if (g.wait) foot.push(`<span class="mini-chip info">⏳ wait</span>`);
    if (gpu) foot.push(`<span class="mini-chip info">GPU ${gpu}</span>`);
    if (model) foot.push(`<span class="mini-chip" title="${esc(model)}">${esc(model)}</span>`);
    if ((g.pending_signals || []).length) foot.push(`<span class="mini-chip warn">⚡${g.pending_signals.length}</span>`);

    return `<div class="node-card kind-goal${sel}${dimmed}" data-key="${esc(n.key)}" style="left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px">
      <div class="node-accent st-${esc(g.status)}"></div>
      <div class="node-body">
        <div class="node-head">
          <span class="dot st-${esc(g.status)}"></span>
          <span class="node-id" title="${esc(g.goal_id)}">${esc(g.goal_id)}</span>
          <span class="node-status st-${esc(g.status)}">${esc(g.status)}</span>
        </div>
        <div class="node-title">${esc(g.objective || '')}</div>
        <div class="node-foot">${foot.join('')}</div>
      </div>
      ${toggleHtml}
    </div>`;
  }

  function applyTransform() {
    const stage = document.getElementById('canvasStage');
    stage.style.transform = `translate(${state.tx}px, ${state.ty}px) scale(${state.scale})`;
    document.getElementById('zoomReadout').textContent = Math.round(state.scale * 100) + '%';
  }

  function fitToView() {
    const vp = document.getElementById('treeContainer');
    const vw = vp.clientWidth || 800;
    const vh = vp.clientHeight || 600;
    const bw = Math.max(1, state.bounds.width);
    const bh = Math.max(1, state.bounds.height);
    const scale = Math.min(1.05, Math.max(0.28, Math.min((vw - 48) / bw, (vh - 64) / bh)));
    state.scale = scale;
    state.tx = Math.round((vw - bw * scale) / 2);
    state.ty = Math.max(24, Math.round((vh - bh * scale) / 2));
    applyTransform();
  }

  function centerOnNode(key) {
    const n = state.nodesByKey.get(key);
    if (!n) return;
    const vp = document.getElementById('treeContainer');
    const vw = vp.clientWidth || 800;
    const vh = vp.clientHeight || 600;
    const targetScale = Math.max(state.scale, 0.85);
    state.scale = Math.min(targetScale, 1.15);
    const cx = n.x + n.w / 2;
    const cy = n.y + n.h / 2;
    state.tx = Math.round(vw / 2 - cx * state.scale);
    state.ty = Math.round(vh / 2 - cy * state.scale);
    applyTransform();
  }

  function renderGraph(opts = {}) {
    const matchedReqs = getFilteredRequests();
    renderReqStrip(matchedReqs);

    const visibleReqs = state.focusedReqId
      ? matchedReqs.filter(r => r.id === state.focusedReqId)
      : matchedReqs;

    const emptyEl = document.getElementById('emptyCanvas');
    const nodesLayer = document.getElementById('nodesLayer');
    const edgesSvg = document.getElementById('edgesSvg');

    if (!visibleReqs.length) {
      state.nodesByKey = new Map();
      state.edges = [];
      nodesLayer.innerHTML = '';
      edgesSvg.innerHTML = '';
      emptyEl.style.display = 'flex';
      renderDetail();
      return;
    }
    emptyEl.style.display = 'none';

    const q = state.query.trim().toLowerCase();
    const trees = visibleReqs.map(r => {
      const root = buildReqTree(r, q);
      measureTree(root);
      return root;
    });

    // Pack multiple Request trees into tidy horizontal shelves
    const allNodes = [];
    const allEdges = [];
    let shelfX = PAD;
    let shelfY = PAD;
    let shelfMaxH = 0;
    let maxRight = PAD;

    for (const root of trees) {
      if (shelfX > PAD && shelfX + root.subW > MAX_SHELF_WIDTH) {
        shelfX = PAD;
        shelfY += shelfMaxH + SHELF_GAP_Y;
        shelfMaxH = 0;
      }
      assignCoords(root, shelfX, shelfY, allNodes, allEdges);
      shelfX += root.subW + TREE_GAP_X;
      if (shelfX - TREE_GAP_X > maxRight) maxRight = shelfX - TREE_GAP_X;
      if (root.subH > shelfMaxH) shelfMaxH = root.subH;
    }

    state.bounds = { width: maxRight + PAD, height: shelfY + shelfMaxH + PAD };
    state.nodesByKey = new Map(allNodes.map(n => [n.key, n]));
    state.edges = allEdges;

    if (!state.selectedKey || !state.nodesByKey.has(state.selectedKey)) {
      state.selectedKey = allNodes[0].key;
    }

    const hlAncestors = ancestorKeysOf(state.selectedKey);

    // Render SVG curved edges
    edgesSvg.setAttribute('width', state.bounds.width);
    edgesSvg.setAttribute('height', state.bounds.height);
    edgesSvg.innerHTML = allEdges.map(({ from, to }) => {
      const x1 = from.x + from.w / 2;
      const y1 = from.y + from.h;
      const x2 = to.x + to.w / 2;
      const y2 = to.y;
      const midY = y1 + (y2 - y1) * 0.52;
      const statusCls = to.kind === 'question' ? 'question' : (to.item.status || '');
      const isHl = hlAncestors.has(to.key) && hlAncestors.has(from.key);
      return `<path class="edge-path ${esc(statusCls)}${isHl ? ' highlight' : ''}" d="M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}" />`;
    }).join('');

    // Render HTML Node Cards
    nodesLayer.innerHTML = allNodes.map(n => renderNodeCardHtml(n, n.key === state.selectedKey)).join('');

    for (const card of nodesLayer.querySelectorAll('.node-card')) {
      card.addEventListener('click', e => {
        if (e.target.closest('.node-toggle')) return;
        e.stopPropagation();
        selectNode(card.dataset.key, { center: false });
      });
      card.addEventListener('dblclick', e => {
        if (e.target.closest('.node-toggle')) return;
        e.stopPropagation();
        const k = card.dataset.key;
        const n = state.nodesByKey.get(k);
        if (n && n.allChildren.length > 0) toggleExpand(k);
      });
    }
    for (const btn of nodesLayer.querySelectorAll('.node-toggle')) {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        toggleExpand(btn.dataset.toggle);
      });
    }

    if (opts.autoFit) fitToView();
    else applyTransform();
    renderDetail();
  }

  function toggleExpand(key) {
    if (state.expanded.has(key)) state.expanded.delete(key);
    else state.expanded.add(key);
    renderGraph({ autoFit: false });
  }

  function selectNode(key, opts = { center: true }) {
    if (key.startsWith('goal:')) {
      const g = goalById.get(key.slice(5));
      if (g) {
        if (state.focusedReqId && state.focusedReqId !== g.request_id) state.focusedReqId = g.request_id;
        state.expanded.add('req:' + g.request_id);
        let p = g.parent_goal_id;
        while (p && p !== 'root') {
          state.expanded.add('goal:' + p);
          const pg = goalById.get(p);
          p = pg ? pg.parent_goal_id : null;
        }
      }
    } else if (key.startsWith('req:')) {
      const rid = key.slice(4);
      if (state.focusedReqId && state.focusedReqId !== rid) state.focusedReqId = rid;
      state.expanded.add(key);
    }
    state.selectedKey = key;
    renderGraph({ autoFit: false });
    if (opts.center) centerOnNode(key);
  }

  // Detail Inspector (Right Pane)
  function renderDetail() {
    const pane = document.getElementById('detailPane');
    const node = state.nodesByKey.get(state.selectedKey);
    if (!node) {
      pane.innerHTML = '<div class="empty-detail">点击左侧树中的任意节点查看详情</div>';
      return;
    }

    if (node.kind === 'req') pane.innerHTML = renderRequestDetail(node.item);
    else if (node.kind === 'question') pane.innerHTML = renderQuestionDetail(node.item, node.req);
    else pane.innerHTML = renderGoalDetail(node.item, node.req);

    for (const el of pane.querySelectorAll('[data-jump]')) {
      el.addEventListener('click', () => selectNode(el.dataset.jump, { center: true }));
    }
    for (const el of pane.querySelectorAll('[data-center]')) {
      el.addEventListener('click', () => centerOnNode(el.dataset.center));
    }
    for (const el of pane.querySelectorAll('[data-focus-req]')) {
      el.addEventListener('click', () => {
        const rid = el.dataset.focusReq;
        state.focusedReqId = state.focusedReqId === rid ? null : rid;
        state.expanded.add('req:' + rid);
        renderGraph({ autoFit: true });
      });
    }
  }

  function renderRequestDetail(r) {
    const allGoals = goalsByReq.get(r.id) || [];
    const l1Goals = childrenByParent.get(`${r.id}::root`) || [];
    const qs = questionsByReq.get(r.id) || [];
    const openQs = qs.filter(q => q.state === 'open');
    const vs = verdictsByReq.get(r.id) || [];
    const notes = notesByReq.get(r.id) || [];
    const totalGpu = allGoals.reduce((acc, g) => acc + activeGpu(g), 0);
    const isFocused = state.focusedReqId === r.id;

    let html = `
      <div class="breadcrumbs"><span>Request</span><span>/</span><span>${esc(r.id.slice(0, 8))}</span></div>
      <div class="detail-header">
        <div>
          <h1 class="detail-title">${esc(r.title || '(无标题)')}</h1>
          <div class="detail-sub">id=${esc(r.id)} · gen=${r.generation || 1} · updated=${fmtTime(r.updated_at)}</div>
        </div>
        <span class="badge st-${esc(r.status)}">${esc(r.status)}</span>
      </div>
      <div class="detail-actions">
        <button class="icon-btn${isFocused ? ' active' : ''}" data-focus-req="${esc(r.id)}">${isFocused ? '退出单树聚焦' : '单树聚焦此需求'}</button>
        <button class="icon-btn" data-center="req:${esc(r.id)}">在画布中居中</button>
      </div>
      <div class="card">
        <div class="card-title">概览</div>
        <div class="kv-grid">
          <div class="kv"><div class="kv-k">状态</div><div class="kv-v">${esc(r.status)}</div></div>
          <div class="kv"><div class="kv-k">Generation</div><div class="kv-v">${r.generation || 1}</div></div>
          <div class="kv"><div class="kv-k">Goal 总数</div><div class="kv-v">${allGoals.length}</div></div>
          <div class="kv"><div class="kv-k">待回答问题</div><div class="kv-v">${openQs.length}</div></div>
          <div class="kv"><div class="kv-k">账面 GPU</div><div class="kv-v">${totalGpu}</div></div>
          <div class="kv"><div class="kv-k">Judge 评审次数</div><div class="kv-v">${vs.length}</div></div>
        </div>
      </div>`;

    if (openQs.length) {
      html += `<div class="card warn-card">
        <div class="card-title">待回答问题 (${openQs.length})</div>
        ${openQs.map(q => `<div class="child-link" data-jump="q:${esc(q.id)}">
          <span>❓ ${esc(q.body || '')}</span>
          <span class="mini-chip warn">open</span>
        </div>`).join('')}
      </div>`;
    }

    if (r.brief) {
      html += `<div class="card">
        <div class="card-title">需求说明 (Brief)</div>
        <div class="prose">${esc(r.brief)}</div>
      </div>`;
    }

    if (l1Goals.length) {
      html += `<div class="card">
        <div class="card-title">一级 Goal (${l1Goals.length})</div>
        ${l1Goals.map(g => `<div class="child-link" data-jump="goal:${esc(g.goal_id)}">
          <div style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            <span class="dot st-${esc(g.status)}" style="display:inline-block;margin-right:6px"></span>
            <strong style="font-family:var(--mono);font-size:12px">${esc(g.goal_id)}</strong>
            <span style="color:var(--fg-soft);margin-left:6px">${esc((g.objective || '').slice(0, 80))}</span>
          </div>
          <span class="node-status st-${esc(g.status)}">${esc(g.status)}</span>
        </div>`).join('')}
      </div>`;
    }

    if (vs.length) {
      html += `<div class="card">
        <div class="card-title">Judge 验收记录 (${vs.length})</div>
        ${vs.map(v => {
          const shared = v.shared_models || [];
          const fb = (v.feedback || []).map(x => `<li>${esc(typeof x === 'string' ? x : JSON.stringify(x))}</li>`).join('');
          return `<details class="fold" ${v === vs[0] ? 'open' : ''}>
            <summary>[${esc(v.decision)}] gen=${v.generation} · ${fmtTime(v.created_at)}</summary>
            ${shared.length ? `<div style="color:var(--warn);font-size:12px;margin-bottom:6px">⚠ 独立性提示：Judge 与产出 Goal 共用了模型 ${esc(shared.join(', '))}</div>` : ''}
            <div class="prose">${esc(v.summary || '')}</div>
            ${fb ? `<ul class="list" style="margin-top:8px">${fb}</ul>` : ''}
          </details>`;
        }).join('')}
      </div>`;
    }

    if (notes.length) {
      html += `<div class="card">
        <div class="card-title">近期工作笔记 (${notes.length})</div>
        ${notes.slice(0, 8).map((n, i) => `<details class="fold" ${i === 0 ? 'open' : ''}>
          <summary>${esc(n.actor || 'note')} · ${fmtTime(n.updated_at || n.created_at)}</summary>
          <div class="prose">${esc(n.body || '')}</div>
        </details>`).join('')}
      </div>`;
    }

    return html;
  }

  function renderQuestionDetail(q, r) {
    return `
      <div class="breadcrumbs">
        <span class="crumb" data-jump="req:${esc(r.id)}">${esc(r.title || r.id.slice(0, 8))}</span>
        <span>/</span><span>Question</span>
      </div>
      <div class="detail-header">
        <div>
          <h1 class="detail-title">❓ 待回答问题</h1>
          <div class="detail-sub">id=${esc(q.id)} · rev=${q.revision || 1} · created=${fmtTime(q.created_at)}</div>
        </div>
        <span class="badge st-question">${esc(q.state)}</span>
      </div>
      <div class="card warn-card">
        <div class="card-title">问题内容</div>
        <div class="prose">${esc(q.body || '')}</div>
      </div>
      ${q.reason ? `<div class="card"><div class="card-title">提问原因 / 上下文</div><div class="prose">${esc(q.reason)}</div></div>` : ''}
      <div class="card">
        <div class="card-title">所属需求</div>
        <div class="child-link" data-jump="req:${esc(r.id)}">
          <span>${esc(r.title || r.id)}</span>
          <span class="node-status st-${esc(r.status)}">${esc(r.status)}</span>
        </div>
      </div>`;
  }

  function renderGoalDetail(g, r) {
    const chain = [];
    let cur = g;
    while (cur) {
      chain.unshift(cur);
      cur = (cur.parent_goal_id && cur.parent_goal_id !== 'root') ? goalById.get(cur.parent_goal_id) : null;
    }
    const crumbs = [
      `<span class="crumb" data-jump="req:${esc(r.id)}">${esc(r.title || r.id.slice(0, 8))}</span>`,
      ...chain.map((x, idx) => idx === chain.length - 1
        ? `<span>${esc(x.goal_id)}</span>`
        : `<span class="crumb" data-jump="goal:${esc(x.goal_id)}">${esc(x.goal_id)}</span>`),
    ].join('<span>/</span>');

    const models = (g.observed_models || []).map(m => m.model).filter(Boolean);
    const modelStr = models.length ? models.join(' → ') : '—';
    const fallback = new Set(models).size > 1;
    const handles = g.external_job_ids || [];
    const childGoals = childrenByParent.get(`${r.id}::${g.goal_id}`) || [];
    const budget = g.budget || {};
    const result = g.result;
    const signals = g.pending_signals || [];

    let html = `
      <div class="breadcrumbs">${crumbs}</div>
      <div class="detail-header">
        <div>
          <h1 class="detail-title">${esc(g.goal_id)}</h1>
          <div class="detail-sub">request=${esc(r.id.slice(0, 8))} · gen=${g.request_generation || 1} · updated=${fmtTime(g.updated_at)}</div>
        </div>
        <span class="badge st-${esc(g.status)}">${esc(g.status)}${g.paused_by_ancestor ? ' (by ' + esc(g.paused_by_ancestor) + ')' : ''}</span>
      </div>
      <div class="detail-actions">
        <button class="icon-btn" data-center="goal:${esc(g.goal_id)}">在画布中居中</button>
      </div>
      <div class="card">
        <div class="card-title">运行属性</div>
        <div class="kv-grid">
          <div class="kv"><div class="kv-k">状态</div><div class="kv-v">${esc(g.status)}</div></div>
          <div class="kv"><div class="kv-k">Category</div><div class="kv-v">${esc(g.category || '—')}</div></div>
          <div class="kv"><div class="kv-k">实际模型${fallback ? ' (fallback)' : ''}</div><div class="kv-v">${esc(modelStr)}</div></div>
          <div class="kv"><div class="kv-k">父节点</div><div class="kv-v">${esc(g.parent_goal_id || 'root')}</div></div>
          <div class="kv"><div class="kv-k">账面 GPU</div><div class="kv-v">${activeGpu(g)}</div></div>
          <div class="kv"><div class="kv-k">创建时间</div><div class="kv-v">${fmtTime(g.created_at)}</div></div>
        </div>
      </div>`;

    if (g.wait) {
      html += `<div class="card info-card">
        <div class="card-title">⏳ 外部等待中 (goal_waits)</div>
        <div class="kv-grid">
          <div class="kv"><div class="kv-k">计划唤醒</div><div class="kv-v">${fmtTime(g.wait.wake_at)}</div></div>
          <div class="kv"><div class="kv-k">监控路径</div><div class="kv-v">${esc(g.wait.watch_path || '—')}</div></div>
        </div>
        ${g.wait.reason ? `<div class="prose" style="margin-top:8px">${esc(g.wait.reason)}</div>` : ''}
      </div>`;
    }

    if (result) {
      const cardCls = result.status === 'blocked' || result.status === 'infeasible' ? ' err-card' : '';
      const issues = (result.issues || []).map(x => `<li>${esc(x)}</li>`).join('');
      const arts = (result.artifacts || []).map(x => `<li><code>${esc(x)}</code></li>`).join('');
      const metrics = result.metrics && Object.keys(result.metrics).length
        ? `<div class="chips" style="margin-top:8px">${Object.entries(result.metrics).map(([k,v]) => `<span class="chip info">${esc(k)}: ${esc(v)}</span>`).join('')}</div>`
        : '';
      html += `<div class="card${cardCls}">
        <div class="card-title">最新汇报 (GoalResult · ${esc(result.status || g.status)})</div>
        <div class="prose">${esc(result.summary || '')}</div>
        ${metrics}
        ${issues ? `<div style="margin-top:10px;font-size:11.5px;color:var(--warn);font-weight:600">问题 / 阻塞</div><ul class="list">${issues}</ul>` : ''}
        ${arts ? `<div style="margin-top:10px;font-size:11.5px;color:var(--muted);font-weight:600">产出物 (Artifacts)</div><ul class="list">${arts}</ul>` : ''}
      </div>`;
    }

    html += `<div class="card">
      <div class="card-title">目标 (Objective)</div>
      <div class="prose">${esc(g.objective || '')}</div>
    </div>`;

    if (g.done_criteria) {
      html += `<div class="card">
        <div class="card-title">完成标准 (Done Criteria)</div>
        <div class="prose">${esc(g.done_criteria)}</div>
      </div>`;
    }

    const lims = Object.entries(budget.limits || {}).map(([k, v]) => `<span class="chip info">${esc(k)}: ${esc(v)}</span>`);
    const allows = Object.entries(budget.allowed || {}).map(([k, v]) => `<span class="chip">${esc(k)}=[${esc((v || []).join(', '))}]</span>`);
    const cons = (budget.constraints || []).map(c => `<span class="chip warn">${esc(c)}</span>`);
    if (lims.length || allows.length || cons.length) {
      html += `<div class="card">
        <div class="card-title">三层预算与约束 (Budget)</div>
        <div class="chips">${lims.join('')}${allows.join('')}${cons.join('')}</div>
      </div>`;
    }

    if (handles.length) {
      html += `<div class="card">
        <div class="card-title">外部作业与资源句柄 (${handles.length})</div>
        <table class="tbl">
          <thead><tr><th>Handle ID</th><th>Backend</th><th>Status</th><th>Usage</th></tr></thead>
          <tbody>${handles.map(h => `<tr>
            <td class="mono">${esc(h.handle_id || '')}</td>
            <td class="mono">${esc(h.backend || 'generic')}</td>
            <td><span class="node-status st-${esc(h.status || 'running')}">${esc(h.status || 'running')}</span></td>
            <td class="mono">${esc(JSON.stringify(h.usage || {}))}</td>
          </tr>`).join('')}</tbody>
        </table>
      </div>`;
    }

    if (childGoals.length) {
      html += `<div class="card">
        <div class="card-title">子 Goal (${childGoals.length})</div>
        ${childGoals.map(c => `<div class="child-link" data-jump="goal:${esc(c.goal_id)}">
          <div style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            <span class="dot st-${esc(c.status)}" style="display:inline-block;margin-right:6px"></span>
            <strong style="font-family:var(--mono);font-size:12px">${esc(c.goal_id)}</strong>
            <span style="color:var(--fg-soft);margin-left:6px">${esc((c.objective || '').slice(0, 72))}</span>
          </div>
          <span class="node-status st-${esc(c.status)}">${esc(c.status)}</span>
        </div>`).join('')}
      </div>`;
    }

    if (signals.length) {
      html += `<div class="card warn-card">
        <div class="card-title">待处理 Signal (${signals.length})</div>
        <ul class="list">${signals.map(s => `<li><code>${esc(s.from || '')}</code>: ${esc(s.message || JSON.stringify(s))}</li>`).join('')}</ul>
      </div>`;
    }

    return html;
  }

  // Pan & Zoom on canvas viewport
  const viewport = document.getElementById('treeContainer');
  let panning = false;
  let panStartX = 0, panStartY = 0, startTx = 0, startTy = 0;

  viewport.addEventListener('mousedown', e => {
    if (e.target.closest('.node-card') || e.target.closest('.canvas-controls')) return;
    panning = true;
    panStartX = e.clientX;
    panStartY = e.clientY;
    startTx = state.tx;
    startTy = state.ty;
    viewport.classList.add('panning');
  });
  window.addEventListener('mousemove', e => {
    if (!panning) return;
    state.tx = startTx + (e.clientX - panStartX);
    state.ty = startTy + (e.clientY - panStartY);
    applyTransform();
  });
  window.addEventListener('mouseup', () => {
    if (!panning) return;
    panning = false;
    viewport.classList.remove('panning');
  });

  viewport.addEventListener('wheel', e => {
    e.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.12 : 0.89;
    const nextScale = Math.min(2.2, Math.max(0.22, state.scale * factor));
    const ratio = nextScale / state.scale;
    state.tx = mx - (mx - state.tx) * ratio;
    state.ty = my - (my - state.ty) * ratio;
    state.scale = nextScale;
    applyTransform();
  }, { passive: false });

  function zoomBy(factor) {
    const rect = viewport.getBoundingClientRect();
    const mx = rect.width / 2;
    const my = rect.height / 2;
    const nextScale = Math.min(2.2, Math.max(0.22, state.scale * factor));
    const ratio = nextScale / state.scale;
    state.tx = mx - (mx - state.tx) * ratio;
    state.ty = my - (my - state.ty) * ratio;
    state.scale = nextScale;
    applyTransform();
  }

  document.getElementById('zoomInBtn').addEventListener('click', () => zoomBy(1.2));
  document.getElementById('zoomOutBtn').addEventListener('click', () => zoomBy(0.83));
  document.getElementById('fitBtn').addEventListener('click', fitToView);

  // Topbar & toolbar bindings
  document.getElementById('searchBox').addEventListener('input', e => {
    state.query = e.target.value;
    renderGraph({ autoFit: true });
  });
  for (const btn of document.querySelectorAll('#scopeFilter button')) {
    btn.addEventListener('click', () => {
      state.scope = btn.dataset.scope;
      state.focusedReqId = null;
      for (const b of document.querySelectorAll('#scopeFilter button')) b.classList.toggle('active', b === btn);
      renderGraph({ autoFit: true });
    });
  }
  document.getElementById('toggleTerminalBtn').addEventListener('click', e => {
    state.hideTerminalGoals = !state.hideTerminalGoals;
    e.currentTarget.classList.toggle('active', state.hideTerminalGoals);
    renderGraph({ autoFit: true });
  });
  document.getElementById('expandAllBtn').addEventListener('click', () => {
    for (const r of DATA.requests || []) state.expanded.add('req:' + r.id);
    for (const g of DATA.goals || []) state.expanded.add('goal:' + g.goal_id);
    renderGraph({ autoFit: true });
  });
  document.getElementById('collapseAllBtn').addEventListener('click', () => {
    state.expanded.clear();
    renderGraph({ autoFit: true });
  });
  document.getElementById('themeBtn').addEventListener('click', () => {
    document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  });

  // Draggable split resizer
  const resizer = document.getElementById('resizer');
  const leftPane = document.getElementById('leftPane');
  let dragging = false;
  resizer.addEventListener('mousedown', () => { dragging = true; resizer.classList.add('dragging'); document.body.style.cursor = 'col-resize'; });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    const pct = Math.min(80, Math.max(30, (e.clientX / window.innerWidth) * 100));
    leftPane.style.width = pct + '%';
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false; resizer.classList.remove('dragging'); document.body.style.cursor = '';
  });

  // Initial render
  for (const b of document.querySelectorAll('#scopeFilter button')) b.classList.toggle('active', b.dataset.scope === state.scope);
  renderTopStats();
  renderGraph({ autoFit: true });
})();
</script>
</body>
</html>
"""


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
