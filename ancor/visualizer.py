"""Deterministic read-only visualizer over requirements.db (?mode=ro).

Generates either a self-contained interactive split-pane Tree + Detail HTML
explorer or a compact Markdown tree summary directly from the authoritative
SQLite database — zero writes, zero LLM calls.
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
    """Render a self-contained interactive split-pane Tree + Detail HTML explorer."""
    snap = load_snapshot(store_path, request_id=request_id)
    # Pre-compute judge independence warnings per verdict
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
<title>ANCOR Explorer</title>
<style>
:root {
  --bg: #0b0f14;
  --surface: #121821;
  --surface-2: #18212e;
  --hover: #1d2838;
  --selected: #1c2e4a;
  --selected-border: #3b82f6;
  --border: #243042;
  --guide: #263347;
  --fg: #e6edf6;
  --fg-soft: #b8c6db;
  --muted: #7f91ab;
  --accent: #60a5fa;
  --ok: #34d399;
  --ok-bg: rgba(52, 211, 153, 0.12);
  --warn: #fbbf24;
  --warn-bg: rgba(251, 191, 36, 0.12);
  --err: #f87171;
  --err-bg: rgba(248, 113, 113, 0.12);
  --info: #38bdf8;
  --info-bg: rgba(56, 189, 248, 0.12);
  --muted-bg: rgba(127, 145, 171, 0.12);
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  --sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Noto Sans SC", sans-serif;
}
[data-theme="light"] {
  --bg: #f5f7fa;
  --surface: #ffffff;
  --surface-2: #f8fafc;
  --hover: #f1f5f9;
  --selected: #e8f1ff;
  --selected-border: #2563eb;
  --border: #e2e8f0;
  --guide: #cbd5e1;
  --fg: #0f172a;
  --fg-soft: #334155;
  --muted: #64748b;
  --accent: #2563eb;
  --ok: #059669;
  --ok-bg: rgba(5, 150, 105, 0.10);
  --warn: #d97706;
  --warn-bg: rgba(217, 119, 6, 0.10);
  --err: #dc2626;
  --err-bg: rgba(220, 38, 38, 0.10);
  --info: #0284c7;
  --info-bg: rgba(2, 132, 199, 0.10);
  --muted-bg: rgba(100, 116, 139, 0.10);
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; overflow: hidden; background: var(--bg); color: var(--fg); font: 13px/1.5 var(--sans); }
button, input { font: inherit; color: inherit; }

/* Top bar */
.topbar {
  height: 50px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  user-select: none;
}
.brand { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 14px; letter-spacing: .02em; white-space: nowrap; }
.brand-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.stats { display: flex; align-items: center; gap: 6px; margin-left: 4px; }
.stat-pill {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600;
  background: var(--surface-2); border: 1px solid var(--border); color: var(--fg-soft);
}
.stat-pill.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 35%, var(--border)); }
.stat-pill.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 35%, var(--border)); }
.stat-pill.info { color: var(--info); border-color: color-mix(in srgb, var(--info) 35%, var(--border)); }

.spacer { flex: 1; }
.search {
  width: 220px; padding: 5px 10px; border-radius: 7px;
  background: var(--surface-2); border: 1px solid var(--border);
  font-size: 12px; outline: none;
}
.search:focus { border-color: var(--accent); }
.seg {
  display: inline-flex; background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 7px; padding: 2px; gap: 2px;
}
.seg button {
  border: 0; background: transparent; padding: 3px 9px; border-radius: 5px;
  font-size: 12px; color: var(--muted); cursor: pointer;
}
.seg button.active { background: var(--selected); color: var(--accent); font-weight: 600; }
.icon-btn {
  border: 1px solid var(--border); background: var(--surface-2); color: var(--fg-soft);
  border-radius: 7px; padding: 4px 9px; font-size: 12px; cursor: pointer; white-space: nowrap;
}
.icon-btn:hover { background: var(--hover); color: var(--fg); }
.icon-btn.active { border-color: var(--accent); color: var(--accent); background: var(--selected); }

/* Split workspace */
.workspace { display: flex; height: calc(100% - 50px); width: 100%; overflow: hidden; }
.left-pane {
  width: 46%; min-width: 280px; max-width: 75%;
  display: flex; flex-direction: column;
  background: var(--surface); border-right: 1px solid var(--border);
}
.tree-toolbar {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 6px 12px; border-bottom: 1px solid var(--border);
  background: var(--surface-2); font-size: 11px; color: var(--muted);
}
.tree-toolbar-actions { display: flex; gap: 6px; }
.link-btn {
  border: 0; background: transparent; color: var(--muted); cursor: pointer;
  padding: 2px 5px; border-radius: 4px; font-size: 11px;
}
.link-btn:hover { background: var(--hover); color: var(--fg); }
.tree-scroll { flex: 1; overflow-y: auto; padding: 6px 0 24px; }

.resizer {
  width: 5px; cursor: col-resize; background: transparent; flex-shrink: 0;
  margin-left: -2px; margin-right: -3px; z-index: 10; transition: background .15s;
}
.resizer:hover, .resizer.dragging { background: var(--accent); opacity: .55; }

.right-pane {
  flex: 1; min-width: 320px; overflow-y: auto; padding: 20px 24px 48px;
  background: var(--bg);
}

/* Tree rows — one level per line, top to bottom */
.tree-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding-right: 12px;
  cursor: pointer;
  border-left: 2px solid transparent;
  white-space: nowrap;
  overflow: hidden;
  user-select: none;
}
.tree-row:hover { background: var(--hover); }
.tree-row.selected { background: var(--selected); border-left-color: var(--selected-border); }
.tree-row.req-row { height: 35px; font-weight: 600; }
.tree-row.terminal-dim { opacity: .68; }
.tree-row.terminal-dim.selected, .tree-row.terminal-dim:hover { opacity: 1; }

.indent-guides { display: inline-flex; height: 100%; flex-shrink: 0; }
.guide-slot { width: 18px; height: 100%; position: relative; flex-shrink: 0; }
.guide-slot::before {
  content: ""; position: absolute; left: 8px; top: 0; bottom: 0; width: 1px; background: var(--guide);
}
.twisty {
  width: 18px; height: 18px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 4px; color: var(--muted); font-size: 11px; transition: transform .12s;
}
.twisty:hover { background: var(--border); color: var(--fg); }
.twisty.open { transform: rotate(90deg); }
.twisty.leaf { visibility: hidden; pointer-events: none; }

.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot.st-active, .dot.st-running, .dot.st-completed, .dot.st-accepted, .dot.st-accept { background: var(--ok); }
.dot.st-running { box-shadow: 0 0 0 3px var(--ok-bg); }
.dot.st-waiting_external, .dot.st-paused, .dot.st-awaiting_acceptance, .dot.st-reject { background: var(--warn); }
.dot.st-blocked, .dot.st-infeasible, .dot.st-cancelled, .dot.st-escalate { background: var(--err); }
.dot.st-question { background: var(--warn); border-radius: 2px; }

.row-id { font-family: var(--mono); font-size: 11.5px; color: var(--accent); flex-shrink: 0; }
.row-title { overflow: hidden; text-overflow: ellipsis; color: var(--fg); flex: 1; min-width: 0; }
.tree-row.goal-row .row-title { color: var(--fg-soft); font-weight: 400; font-size: 12px; }
.row-meta { display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0; margin-left: 6px; }
.mini-tag {
  font-family: var(--mono); font-size: 10px; padding: 1px 5px; border-radius: 4px;
  background: var(--muted-bg); color: var(--muted);
}
.mini-tag.ok { background: var(--ok-bg); color: var(--ok); }
.mini-tag.warn { background: var(--warn-bg); color: var(--warn); }
.mini-tag.err { background: var(--err-bg); color: var(--err); }
.mini-tag.info { background: var(--info-bg); color: var(--info); }

/* Right detail pane */
.empty-state {
  height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center;
  color: var(--muted); gap: 6px; text-align: center;
}
.breadcrumb {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
  font-size: 12px; color: var(--muted); margin-bottom: 12px;
}
.crumb { cursor: pointer; color: var(--accent); }
.crumb:hover { text-decoration: underline; }
.detail-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
  padding-bottom: 14px; border-bottom: 1px solid var(--border); margin-bottom: 16px;
}
.detail-title { margin: 0; font-size: 18px; line-height: 1.35; }
.detail-sub { margin-top: 4px; font-family: var(--mono); font-size: 11.5px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 10px; }
.badge {
  display: inline-flex; align-items: center; gap: 5px; flex-shrink: 0;
  font-size: 11.5px; font-weight: 600; padding: 3px 10px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--surface);
}
.badge.st-active, .badge.st-running, .badge.st-completed, .badge.st-accepted, .badge.st-accept { color: var(--ok); background: var(--ok-bg); border-color: color-mix(in srgb, var(--ok) 40%, transparent); }
.badge.st-waiting_external, .badge.st-paused, .badge.st-awaiting_acceptance, .badge.st-reject { color: var(--warn); background: var(--warn-bg); border-color: color-mix(in srgb, var(--warn) 40%, transparent); }
.badge.st-blocked, .badge.st-infeasible, .badge.st-cancelled, .badge.st-escalate { color: var(--err); background: var(--err-bg); border-color: color-mix(in srgb, var(--err) 40%, transparent); }

.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 14px;
}
.card.alert-warn { border-color: color-mix(in srgb, var(--warn) 50%, var(--border)); background: color-mix(in srgb, var(--warn-bg) 55%, var(--surface)); }
.card.alert-err { border-color: color-mix(in srgb, var(--err) 50%, var(--border)); background: color-mix(in srgb, var(--err-bg) 55%, var(--surface)); }
.card-title {
  font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); margin: 0 0 10px; display: flex; align-items: center; justify-content: space-between;
}
.kv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; }
.kv { background: var(--surface-2); border: 1px solid var(--border); border-radius: 7px; padding: 8px 10px; }
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
  <div class="brand"><span class="brand-dot"></span>ANCOR Explorer</div>
  <div class="stats" id="topStats"></div>
  <div class="spacer"></div>
  <input id="searchBox" class="search" type="search" placeholder="搜索需求 / Goal / 摘要…">
  <div class="seg" id="scopeFilter">
    <button data-scope="open" class="active">进行中</button>
    <button data-scope="all">全部</button>
    <button data-scope="questions">待回答</button>
  </div>
  <button id="toggleTerminalBtn" class="icon-btn" title="隐藏/显示已终态 Goal">隐藏终态 Goal</button>
  <button id="themeBtn" class="icon-btn" title="切换明暗主题">☀/☾</button>
</header>

<div class="workspace">
  <aside class="left-pane" id="leftPane">
    <div class="tree-toolbar">
      <span id="treeCount">0 项</span>
      <div class="tree-toolbar-actions">
        <button class="link-btn" id="expandAllBtn">全部展开</button>
        <button class="link-btn" id="collapseAllBtn">全部折叠</button>
      </div>
    </div>
    <div class="tree-scroll" id="treeContainer" tabindex="0"></div>
  </aside>
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

  // Indexing
  const reqById = new Map();
  const goalById = new Map();
  const goalsByReq = new Map();
  const childrenByParent = new Map(); // key: `${reqId}::${parentGoalId}`
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

  // Sort goals: non-terminal first, then newest updated_at
  const goalRank = s => (s === 'running' ? 0 : s === 'waiting_external' ? 1 : s === 'paused' ? 2 : s === 'blocked' ? 3 : 4);
  for (const arr of childrenByParent.values()) {
    arr.sort((a, b) => (goalRank(a.status) - goalRank(b.status)) || ((b.updated_at || 0) - (a.updated_at || 0)));
  }

  // State
  const state = {
    scope: (DATA.requests || []).some(r => !TERMINAL_REQ.has(r.status)) ? 'open' : 'all',
    hideTerminalGoals: false,
    query: '',
    expanded: new Set(),
    selectedKey: null,
    visibleRows: [],
  };

  // Default expand active requests
  for (const r of DATA.requests || []) {
    if (!TERMINAL_REQ.has(r.status)) state.expanded.add('req:' + r.id);
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

  // Top stats
  function renderTopStats() {
    const openReqs = (DATA.requests || []).filter(r => !TERMINAL_REQ.has(r.status)).length;
    const runningGoals = (DATA.goals || []).filter(g => g.status === 'running' || g.status === 'waiting_external').length;
    const openQs = (DATA.questions || []).filter(q => q.state === 'open').length;
    let totalGpu = 0;
    for (const g of DATA.goals || []) totalGpu += activeGpu(g);
    document.getElementById('topStats').innerHTML = [
      `<span class="stat-pill ok">活跃需求 ${openReqs}</span>`,
      `<span class="stat-pill info">运行/等待 Goal ${runningGoals}</span>`,
      openQs ? `<span class="stat-pill warn">待答问题 ${openQs}</span>` : `<span class="stat-pill">待答问题 0</span>`,
      `<span class="stat-pill">账面 GPU ${totalGpu}</span>`,
    ].join('');
  }

  // Filter + flatten tree rows
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

  function buildRows() {
    const q = state.query.trim().toLowerCase();
    const rows = [];

    const reqOrder = s => (s === 'active' ? 0 : s === 'awaiting_acceptance' ? 1 : s === 'paused' ? 2 : s === 'accepted' ? 3 : 4);
    const reqs = [...(DATA.requests || [])].sort((a, b) => reqOrder(a.status) - reqOrder(b.status));

    for (const r of reqs) {
      const openQs = (questionsByReq.get(r.id) || []).filter(x => x.state === 'open');
      if (state.scope === 'open' && TERMINAL_REQ.has(r.status)) continue;
      if (state.scope === 'questions' && openQs.length === 0) continue;

      const allReqGoals = goalsByReq.get(r.id) || [];
      const selfMatch = reqMatchesQuery(r, q);
      const anyGoalMatch = q && allReqGoals.some(g => goalMatchesQuery(g, q));
      if (q && !selfMatch && !anyGoalMatch) continue;

      const rootGoals = (childrenByParent.get(`${r.id}::root`) || []).filter(g => !state.hideTerminalGoals || !TERMINAL_GOAL.has(g.status));
      const hasChildren = rootGoals.length > 0 || openQs.length > 0;
      const key = 'req:' + r.id;
      const isExpanded = Boolean(q && anyGoalMatch) || state.expanded.has(key);

      rows.push({ key, kind: 'req', depth: 0, item: r, req: r, hasChildren, isExpanded, openQsCount: openQs.length, goalCount: allReqGoals.length });

      if (hasChildren && isExpanded) {
        // Show open questions right under the request
        for (const qItem of openQs) {
          rows.push({ key: 'q:' + qItem.id, kind: 'question', depth: 1, item: qItem, req: r, hasChildren: false, isExpanded: false });
        }
        // Walk goal hierarchy recursively — one level per row
        function walkGoals(parentId, depth) {
          const kids = (childrenByParent.get(`${r.id}::${parentId}`) || []).filter(g => !state.hideTerminalGoals || !TERMINAL_GOAL.has(g.status));
          for (const g of kids) {
            const subKids = (childrenByParent.get(`${r.id}::${g.goal_id}`) || []).filter(c => !state.hideTerminalGoals || !TERMINAL_GOAL.has(c.status));
            const gKey = 'goal:' + g.goal_id;
            const gExpanded = Boolean(q) || state.expanded.has(gKey);
            rows.push({ key: gKey, kind: 'goal', depth, item: g, req: r, hasChildren: subKids.length > 0, isExpanded: gExpanded });
            if (subKids.length > 0 && gExpanded) walkGoals(g.goal_id, depth + 1);
          }
        }
        walkGoals('root', 1);
      }
    }
    state.visibleRows = rows;
    return rows;
  }

  function renderTree() {
    const rows = buildRows();
    if (!state.selectedKey || !rows.some(r => r.key === state.selectedKey)) {
      state.selectedKey = rows.length ? rows[0].key : null;
    }
    const container = document.getElementById('treeContainer');
    document.getElementById('treeCount').textContent = `${rows.length} 行`;

    if (!rows.length) {
      container.innerHTML = '<div class="empty-state" style="padding:32px">没有匹配的节点</div>';
      renderDetail();
      return;
    }

    container.innerHTML = rows.map(row => {
      const guides = '<span class="guide-slot"></span>'.repeat(row.depth);
      const twistyCls = row.hasChildren ? (row.isExpanded ? 'twisty open' : 'twisty') : 'twisty leaf';
      const selCls = row.key === state.selectedKey ? ' selected' : '';

      if (row.kind === 'req') {
        const r = row.item;
        const dim = TERMINAL_REQ.has(r.status) ? ' terminal-dim' : '';
        const tags = [];
        if (row.openQsCount) tags.push(`<span class="mini-tag warn">?${row.openQsCount}</span>`);
        if (row.goalCount) tags.push(`<span class="mini-tag">${row.goalCount}g</span>`);
        tags.push(`<span class="mini-tag">${esc(r.status)}</span>`);
        return `<div class="tree-row req-row${dim}${selCls}" data-key="${esc(row.key)}">
          <span class="indent-guides">${guides}</span>
          <span class="${twistyCls}" data-toggle="${esc(row.key)}">▸</span>
          <span class="dot st-${esc(r.status)}"></span>
          <span class="row-id">${esc(r.id.slice(0, 8))}</span>
          <span class="row-title" title="${esc(r.title)}">${esc(r.title)}</span>
          <span class="row-meta">${tags.join('')}</span>
        </div>`;
      }

      if (row.kind === 'question') {
        const qItem = row.item;
        return `<div class="tree-row goal-row${selCls}" data-key="${esc(row.key)}">
          <span class="indent-guides">${guides}</span>
          <span class="twisty leaf">▸</span>
          <span class="dot st-question"></span>
          <span class="row-id" style="color:var(--warn)">Q</span>
          <span class="row-title" title="${esc(qItem.body)}">${esc(qItem.id)}: ${esc(qItem.body)}</span>
          <span class="row-meta"><span class="mini-tag warn">open</span></span>
        </div>`;
      }

      const g = row.item;
      const dim = TERMINAL_GOAL.has(g.status) ? ' terminal-dim' : '';
      const gpu = activeGpu(g);
      const tags = [];
      if (gpu > 0) tags.push(`<span class="mini-tag info">gpu:${gpu}</span>`);
      if (g.wait) tags.push(`<span class="mini-tag warn">wait</span>`);
      const stCls = (g.status === 'running' || g.status === 'completed') ? 'ok' : (g.status === 'waiting_external' || g.status === 'paused') ? 'warn' : 'err';
      tags.push(`<span class="mini-tag ${stCls}">${esc(g.status)}</span>`);
      return `<div class="tree-row goal-row${dim}${selCls}" data-key="${esc(row.key)}">
        <span class="indent-guides">${guides}</span>
        <span class="${twistyCls}" data-toggle="${esc(row.key)}">▸</span>
        <span class="dot st-${esc(g.status)}"></span>
        <span class="row-id">${esc(g.goal_id)}</span>
        <span class="row-title" title="${esc(g.objective)}">${esc(g.objective)}</span>
        <span class="row-meta">${tags.join('')}</span>
      </div>`;
    }).join('');

    renderDetail();
  }

  function selectAndReveal(key) {
    if (key.startsWith('goal:')) {
      const g = goalById.get(key.slice(5));
      if (g) {
        state.expanded.add('req:' + g.request_id);
        let cur = g;
        while (cur && cur.parent_goal_id && cur.parent_goal_id !== 'root') {
          state.expanded.add('goal:' + cur.parent_goal_id);
          cur = goalById.get(cur.parent_goal_id);
        }
      }
    }
    state.selectedKey = key;
    renderTree();
  }

  function renderDetail() {
    const pane = document.getElementById('detailPane');
    const row = state.visibleRows.find(r => r.key === state.selectedKey);
    if (!row) {
      pane.innerHTML = '<div class="empty-state"><h3>选择一个节点查看详情</h3><span>点击左侧树中的需求、问题或 Goal</span></div>';
      return;
    }
    if (row.kind === 'req') { pane.innerHTML = renderRequestDetail(row.item); return; }
    if (row.kind === 'question') { pane.innerHTML = renderQuestionDetail(row.item, row.req); return; }
    pane.innerHTML = renderGoalDetail(row.item, row.req);
  }

  function renderRequestDetail(req) {
    const rGoals = goalsByReq.get(req.id) || [];
    const qs = questionsByReq.get(req.id) || [];
    const openQs = qs.filter(q => q.state === 'open');
    const verdicts = verdictsByReq.get(req.id) || [];
    const notes = notesByReq.get(req.id) || [];

    const counts = { running: 0, waiting_external: 0, completed: 0, blocked: 0 };
    for (const g of rGoals) if (g.status in counts) counts[g.status]++;

    const openQHtml = openQs.length ? `<div class="card alert-warn">
      <div class="card-title"><span>待回答问题 (${openQs.length})</span></div>
      ${openQs.map(q => `<div style="margin-bottom:10px">
        <div style="font-family:var(--mono);font-size:12px;font-weight:700;color:var(--warn)">${esc(q.id)}</div>
        <p class="prose" style="margin-top:4px">${esc(q.body)}</p>
      </div>`).join('<hr style="border:0;border-top:1px solid var(--border);margin:10px 0">')}
    </div>` : '';

    const l1Goals = childrenByParent.get(`${req.id}::root`) || [];
    const goalsListHtml = l1Goals.length ? `<div class="card">
      <div class="card-title"><span>L1 Goals (${l1Goals.length})</span><span>running ${counts.running} · waiting ${counts.waiting_external} · completed ${counts.completed} · blocked ${counts.blocked}</span></div>
      ${l1Goals.map(g => `<div class="child-link" data-jump="goal:${esc(g.goal_id)}">
        <div style="display:flex;align-items:center;gap:8px;min-width:0">
          <span class="dot st-${esc(g.status)}"></span>
          <span class="row-id">${esc(g.goal_id)}</span>
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg-soft)">${esc(g.objective)}</span>
        </div>
        <span class="mini-tag">${esc(g.status)}</span>
      </div>`).join('')}
    </div>` : `<div class="card"><div class="card-title">L1 Goals</div><span style="color:var(--muted)">暂无 Goal 节点</span></div>`;

    const verdictHtml = verdicts.length ? `<div class="card">
      <div class="card-title">Judge Verdicts (${verdicts.length})</div>
      ${verdicts.slice(0, 5).map(v => {
        const models = (v.runtime || []).map(m => m.model).filter(Boolean).join(', ');
        const shared = (v.shared_models || []).join(', ');
        return `<div style="padding:8px 0;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span class="badge st-${esc(v.decision)}">${esc(v.decision)}</span>
            <span style="font-family:var(--mono);font-size:11px;color:var(--muted)">gen ${v.generation} · ${fmtTime(v.created_at)}</span>
            ${models ? `<span class="chip info">${esc(models)}</span>` : ''}
            ${shared ? `<span class="chip warn">⚠ 共享模型: ${esc(shared)}</span>` : ''}
          </div>
          <p class="prose">${esc(v.summary)}</p>
        </div>`;
      }).join('')}
    </div>` : '';

    const notesHtml = notes.length ? `<div class="card">
      <div class="card-title">最近工作笔记 (${notes.length})</div>
      ${notes.slice(0, 6).map(n => `<details class="fold">
        <summary>${esc((n.id || '').slice(0, 8))} · ${esc(n.actor || 'root')} · ${fmtTime(n.updated_at || n.created_at)}</summary>
        <p class="prose">${esc(n.body)}</p>
      </details>`).join('')}
    </div>` : '';

    return `
      <div class="breadcrumb"><span>Request</span><span>›</span><span>${esc(req.id.slice(0, 8))}</span></div>
      <div class="detail-head">
        <div>
          <h1 class="detail-title">${esc(req.title)}</h1>
          <div class="detail-sub">
            <span>ID: ${esc(req.id)}</span>
            <span>gen: ${req.generation ?? 1} (observed: ${req.observed_generation ?? 0})</span>
            ${req.target_status ? `<span>target: ${esc(req.target_status)}</span>` : ''}
          </div>
        </div>
        <span class="badge st-${esc(req.status)}">${esc(req.status)}</span>
      </div>
      ${openQHtml}
      ${req.brief ? `<div class="card"><div class="card-title">Brief</div><p class="prose">${esc(req.brief)}</p></div>` : ''}
      ${goalsListHtml}
      ${verdictHtml}
      ${notesHtml}
    `;
  }

  function renderQuestionDetail(q, req) {
    return `
      <div class="breadcrumb">
        <span class="crumb" data-jump="req:${esc(req.id)}">${esc(req.title)}</span>
        <span>›</span><span>Open Question</span>
      </div>
      <div class="detail-head">
        <div>
          <h1 class="detail-title">${esc(q.id)}</h1>
          <div class="detail-sub">
            <span>request: ${esc(req.id)}</span>
            <span>revision: ${q.revision ?? 1}</span>
            ${q.created_at ? `<span>created: ${fmtTime(q.created_at)}</span>` : ''}
          </div>
        </div>
        <span class="badge st-waiting_external">${esc(q.state)}</span>
      </div>
      <div class="card alert-warn">
        <div class="card-title">问题正文</div>
        <p class="prose">${esc(q.body)}</p>
      </div>
      ${q.reason ? `<div class="card"><div class="card-title">提问原因</div><p class="prose">${esc(q.reason)}</p></div>` : ''}
    `;
  }

  function renderGoalDetail(g, req) {
    // Breadcrumb chain
    const chain = [];
    let cur = g;
    while (cur) {
      chain.unshift(cur);
      cur = (cur.parent_goal_id && cur.parent_goal_id !== 'root') ? goalById.get(cur.parent_goal_id) : null;
    }
    const crumbs = [
      `<span class="crumb" data-jump="req:${esc(req.id)}">${esc(req.title)}</span>`,
      ...chain.map((n, idx) => idx === chain.length - 1
        ? `<span>${esc(n.goal_id)}</span>`
        : `<span class="crumb" data-jump="goal:${esc(n.goal_id)}">${esc(n.goal_id)}</span>`)
    ].join(' <span>›</span> ');

    const models = (g.observed_models || []).map(m => `${m.model || '?'}${m.provider ? ' (' + m.provider + ')' : ''}`);
    const budget = g.budget || {};
    const limits = budget.limits || {};
    const allowed = budget.allowed || {};
    const constraints = budget.constraints || [];
    const handles = g.external_job_ids || [];
    const children = childrenByParent.get(`${req.id}::${g.goal_id}`) || [];
    const res = g.result;

    const waitHtml = g.wait ? `<div class="card alert-warn">
      <div class="card-title">定时 / 文件唤醒等待中 (ancor_goal_wait)</div>
      <div class="kv-grid">
        <div class="kv"><div class="kv-k">Wake At</div><div class="kv-v">${fmtTime(g.wait.wake_at)}</div></div>
        <div class="kv"><div class="kv-k">Watch Path</div><div class="kv-v">${esc(g.wait.watch_path || '—')}</div></div>
      </div>
      ${g.wait.reason ? `<p class="prose" style="margin-top:8px">${esc(g.wait.reason)}</p>` : ''}
    </div>` : '';

    const resultHtml = res ? `<div class="card ${res.status === 'blocked' || res.status === 'infeasible' ? 'alert-err' : ''}">
      <div class="card-title"><span>Goal Result (${esc(res.status)})</span><span>gen ${res.request_generation ?? g.generation}</span></div>
      <p class="prose">${esc(res.summary)}</p>
      ${(res.open_issues && res.open_issues.length) ? `<div style="margin-top:10px"><div class="kv-k">Open Issues</div><ul class="list">${res.open_issues.map(i => `<li>${esc(i)}</li>`).join('')}</ul></div>` : ''}
      ${(res.artifacts && res.artifacts.length) ? `<div style="margin-top:10px"><div class="kv-k">Artifacts</div><table class="tbl"><thead><tr><th>Path</th><th>Description</th></tr></thead><tbody>${res.artifacts.map(a => `<tr><td class="mono">${esc(a.path)}</td><td>${esc(a.desc)}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${(res.metrics && Object.keys(res.metrics).length) ? `<div style="margin-top:10px"><div class="kv-k">Metrics</div><div class="chips" style="margin-top:4px">${Object.entries(res.metrics).map(([k,v]) => `<span class="chip info">${esc(k)}: ${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</span>`).join('')}</div></div>` : ''}
    </div>` : '';

    const handlesHtml = handles.length ? `<div class="card">
      <div class="card-title">External Handles (${handles.length})</div>
      <table class="tbl"><thead><tr><th>Handle ID</th><th>Status</th><th>Usage</th><th>Detail</th></tr></thead><tbody>
      ${handles.map(h => `<tr>
        <td class="mono">${esc(h.handle_id || h.job_id || '?')}</td>
        <td><span class="mini-tag ${ACTIVE_HANDLE.has(h.status || 'running') ? 'warn' : ''}">${esc(h.status || 'running')}</span></td>
        <td class="mono">${esc(JSON.stringify(h.usage || {}))}</td>
        <td class="mono">${esc(String(h.detail || '—').slice(0, 140))}</td>
      </tr>`).join('')}
      </tbody></table>
    </div>` : '';

    const childrenHtml = children.length ? `<div class="card">
      <div class="card-title">子 Goals (${children.length})</div>
      ${children.map(c => `<div class="child-link" data-jump="goal:${esc(c.goal_id)}">
        <div style="display:flex;align-items:center;gap:8px;min-width:0">
          <span class="dot st-${esc(c.status)}"></span>
          <span class="row-id">${esc(c.goal_id)}</span>
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg-soft)">${esc(c.objective)}</span>
        </div>
        <span class="mini-tag">${esc(c.status)}</span>
      </div>`).join('')}
    </div>` : '';

    return `
      <div class="breadcrumb">${crumbs}</div>
      <div class="detail-head">
        <div>
          <h1 class="detail-title" style="font-family:var(--mono)">${esc(g.goal_id)}</h1>
          <div class="detail-sub">
            <span>parent: ${esc(g.parent_goal_id || 'root')}</span>
            <span>gen: ${g.generation}</span>
            <span>updated: ${fmtTime(g.updated_at)}</span>
            ${g.pid ? `<span>PID: ${g.pid}</span>` : ''}
            ${g.paused_by_ancestor ? `<span style="color:var(--warn)">paused by ${esc(g.paused_by_ancestor)}</span>` : ''}
          </div>
        </div>
        <span class="badge st-${esc(g.status)}">${esc(g.status)}</span>
      </div>

      <div class="card">
        <div class="card-title">运行属性</div>
        <div class="kv-grid">
          <div class="kv"><div class="kv-k">Category</div><div class="kv-v">${esc(g.category || 'default')}</div></div>
          <div class="kv"><div class="kv-k">Observed Models</div><div class="kv-v">${esc(models.length ? models.join(' → ') : '—')}</div></div>
          <div class="kv"><div class="kv-k">Created</div><div class="kv-v">${fmtTime(g.created_at)}</div></div>
          <div class="kv"><div class="kv-k">Deadline</div><div class="kv-v">${fmtTime(g.deadline_at)}</div></div>
        </div>
      </div>

      ${waitHtml}
      ${resultHtml}

      <div class="card">
        <div class="card-title">Objective</div>
        <p class="prose">${esc(g.objective)}</p>
        ${g.done_criteria ? `<div class="card-title" style="margin-top:14px">Done Criteria</div><p class="prose">${esc(g.done_criteria)}</p>` : ''}
      </div>

      <div class="card">
        <div class="card-title">3-Layer Budget</div>
        <div class="chips">
          ${Object.entries(limits).map(([k,v]) => `<span class="chip info">${esc(k)}: ${esc(v)}</span>`).join('')}
          ${Object.entries(allowed).map(([k,v]) => `<span class="chip ok">${esc(k)}=[${esc(Array.isArray(v) ? v.join(', ') : v)}]</span>`).join('')}
          ${(!Object.keys(limits).length && !Object.keys(allowed).length) ? '<span style="color:var(--muted)">无数值/白名单限制</span>' : ''}
        </div>
        ${constraints.length ? `<ul class="list" style="margin-top:10px">${constraints.map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}
      </div>

      ${handlesHtml}
      ${childrenHtml}

      ${(g.pending_signals && g.pending_signals.length) ? `<details class="fold">
        <summary>Pending Signals (${g.pending_signals.length})</summary>
        <pre class="prose" style="font-family:var(--mono);font-size:11.5px">${esc(JSON.stringify(g.pending_signals, null, 2))}</pre>
      </details>` : ''}
    `;
  }

  // Event bindings
  const treeEl = document.getElementById('treeContainer');
  treeEl.addEventListener('click', e => {
    const toggle = e.target.closest('[data-toggle]');
    if (toggle) {
      e.stopPropagation();
      const k = toggle.dataset.toggle;
      if (state.expanded.has(k)) state.expanded.delete(k); else state.expanded.add(k);
      renderTree();
      return;
    }
    const rowEl = e.target.closest('.tree-row');
    if (rowEl) {
      state.selectedKey = rowEl.dataset.key;
      renderTree();
    }
  });
  treeEl.addEventListener('dblclick', e => {
    const rowEl = e.target.closest('.tree-row');
    if (!rowEl) return;
    const k = rowEl.dataset.key;
    if (state.expanded.has(k)) state.expanded.delete(k); else state.expanded.add(k);
    renderTree();
  });
  treeEl.addEventListener('keydown', e => {
    const idx = state.visibleRows.findIndex(r => r.key === state.selectedKey);
    if (idx < 0) return;
    const cur = state.visibleRows[idx];
    if (e.key === 'ArrowDown' && idx + 1 < state.visibleRows.length) {
      e.preventDefault(); state.selectedKey = state.visibleRows[idx + 1].key; renderTree();
    } else if (e.key === 'ArrowUp' && idx > 0) {
      e.preventDefault(); state.selectedKey = state.visibleRows[idx - 1].key; renderTree();
    } else if (e.key === 'ArrowRight' && cur.hasChildren && !cur.isExpanded) {
      e.preventDefault(); state.expanded.add(cur.key); renderTree();
    } else if (e.key === 'ArrowLeft' && cur.hasChildren && cur.isExpanded) {
      e.preventDefault(); state.expanded.delete(cur.key); renderTree();
    }
  });

  document.getElementById('detailPane').addEventListener('click', e => {
    const jump = e.target.closest('[data-jump]');
    if (jump) selectAndReveal(jump.dataset.jump);
  });

  document.getElementById('searchBox').addEventListener('input', e => {
    state.query = e.target.value;
    renderTree();
  });

  document.getElementById('scopeFilter').addEventListener('click', e => {
    const btn = e.target.closest('button[data-scope]');
    if (!btn) return;
    state.scope = btn.dataset.scope;
    for (const b of document.querySelectorAll('#scopeFilter button')) b.classList.toggle('active', b === btn);
    renderTree();
  });

  const termBtn = document.getElementById('toggleTerminalBtn');
  termBtn.addEventListener('click', () => {
    state.hideTerminalGoals = !state.hideTerminalGoals;
    termBtn.classList.toggle('active', state.hideTerminalGoals);
    renderTree();
  });

  document.getElementById('expandAllBtn').addEventListener('click', () => {
    for (const r of DATA.requests || []) state.expanded.add('req:' + r.id);
    for (const g of DATA.goals || []) state.expanded.add('goal:' + g.goal_id);
    renderTree();
  });
  document.getElementById('collapseAllBtn').addEventListener('click', () => {
    state.expanded.clear();
    renderTree();
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
    const pct = Math.min(72, Math.max(22, (e.clientX / window.innerWidth) * 100));
    leftPane.style.width = pct + '%';
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false; resizer.classList.remove('dragging'); document.body.style.cursor = '';
  });

  // Initial scope button sync
  for (const b of document.querySelectorAll('#scopeFilter button')) b.classList.toggle('active', b.dataset.scope === state.scope);
  renderTopStats();
  renderTree();
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
