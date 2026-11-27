"""Sidecar active_goals store, structured /goal contracts, generic resource attenuation & tree cascade.

The alignment layer (requests, point_versions, changes, grants, questions) stays
authoritative and untouched. This module owns the thin deterministic execution
substrate:
  - active_goals topology, generic 3-layer budgets (limits / allowed / constraints),
    sessions, PIDs and external job/resource handles
  - monotonic capability & resource attenuation on spawn (open-ended key-based
    checks + inherited natural-language constraints)
  - deterministic tree-cascading pause(drain|preempt), resume and stop/cancel
  - structured Goal / GoalResult validation and parent wakeup routing
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import time
from uuid import uuid4

from .model_routing import CategoryError, normalize_category

ACTIVE_GOAL_STATUSES = frozenset({'running', 'waiting_external', 'paused'})
TERMINAL_GOAL_STATUSES = frozenset({'completed', 'blocked', 'infeasible', 'cancelled'})
ALL_GOAL_STATUSES = ACTIVE_GOAL_STATUSES | TERMINAL_GOAL_STATUSES
NON_TERMINAL_BUDGET_STATUSES = frozenset({'running', 'waiting_external', 'paused'})
ACTIVE_HANDLE_STATUSES = frozenset({'pending', 'running', 'queued', 'active'})
ACTIVE_JOB_STATUSES = ACTIVE_HANDLE_STATUSES


class AttenuationError(ValueError):
    """Raised when a child /goal violates monotonic capability or resource attenuation."""


class QuotaExceededError(RuntimeError):
    """Raised when resource acquisition exceeds a goal or ancestor limit/allowlist."""


def init_goal_tables(db) -> None:
    """Initialize sidecar execution-plane tables inside requirements.db."""
    db.executescript('''
        CREATE TABLE IF NOT EXISTS active_goals (
            goal_id TEXT PRIMARY KEY,
            parent_goal_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            objective TEXT NOT NULL,
            done_criteria TEXT NOT NULL,
            scope TEXT NOT NULL,
            budget TEXT NOT NULL,
            session_id TEXT NOT NULL,
            pid INTEGER,
            external_job_ids TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            paused_by_ancestor TEXT,
            needs_wake INTEGER NOT NULL DEFAULT 1,
            pending_signals TEXT NOT NULL DEFAULT '[]',
            result_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            deadline_at REAL,
            category TEXT,
            observed_models TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS active_goals_parent ON active_goals(parent_goal_id);
        CREATE INDEX IF NOT EXISTS active_goals_request ON active_goals(request_id, status);
        CREATE INDEX IF NOT EXISTS active_goals_status ON active_goals(status, needs_wake);

        CREATE TABLE IF NOT EXISTS judge_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            decision TEXT NOT NULL,
            summary TEXT NOT NULL,
            feedback TEXT NOT NULL DEFAULT '[]',
            artifacts TEXT NOT NULL DEFAULT '[]',
            evidence TEXT,
            attempt INTEGER NOT NULL DEFAULT 1,
            runtime TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS judge_verdicts_request
            ON judge_verdicts(request_id, generation, id);
    ''')


def default_process_killer(pid: int) -> bool:
    """Best-effort termination of a local Hermes goal process group / PID."""
    if not pid or pid <= 0:
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _nonempty_str(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a non-empty string')
    return value.strip()


def _is_path_key(key: str) -> bool:
    return key == 'paths' or key.endswith('_paths') or key.endswith('.paths')


def _is_non_additive_limit(key: str) -> bool:
    return key == 'wall_hours' or key.endswith('_hours') or key.startswith('max_single_')


def _normalize_path(raw: str) -> str:
    text = _nonempty_str(raw, 'path entry')
    return str(Path(text).expanduser().resolve())


def _path_within(child_path: str, parent_paths: list[str]) -> bool:
    child = Path(child_path).expanduser().resolve()
    for candidate in parent_paths:
        parent = Path(candidate).expanduser().resolve()
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            continue
    return False


def _parse_budget(raw_budget: dict | None, extra_writable_paths: list[str] | None = None) -> dict:
    """Parse generic 3-layer budget: limits (numeric), allowed (allowlists), constraints (natural language)."""
    if raw_budget is None:
        raw_budget = {}
    if not isinstance(raw_budget, dict):
        raise ValueError('budget must be an object')

    raw_limits = raw_budget.get('limits') or {}
    if not isinstance(raw_limits, dict):
        raise ValueError('budget.limits must be an object mapping resource keys to numbers')
    limits: dict[str, float | int] = {}
    for k, v in raw_limits.items():
        key = _nonempty_str(k, 'budget.limits key')
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f'budget.limits[{key!r}] must be a non-negative number')
        if key == 'wall_hours' and v <= 0:
            raise ValueError('budget.limits["wall_hours"] must be positive')
        limits[key] = v

    raw_allowed = raw_budget.get('allowed') or {}
    if not isinstance(raw_allowed, dict):
        raise ValueError('budget.allowed must be an object mapping category keys to string lists')
    allowed: dict[str, list[str]] = {}
    for k, vals in raw_allowed.items():
        key = _nonempty_str(k, 'budget.allowed key')
        if not isinstance(vals, list):
            raise ValueError(f'budget.allowed[{key!r}] must be a list of strings')
        if _is_path_key(key):
            allowed[key] = list(dict.fromkeys(_normalize_path(p) for p in vals))
        else:
            allowed[key] = list(dict.fromkeys(_nonempty_str(v, f'budget.allowed[{key!r}]') for v in vals))

    if extra_writable_paths:
        merged = list(dict.fromkeys([
            *allowed.get('writable_paths', []),
            *(_normalize_path(p) for p in extra_writable_paths),
        ]))
        allowed['writable_paths'] = merged

    raw_constraints = raw_budget.get('constraints') or []
    if not isinstance(raw_constraints, list):
        raise ValueError('budget.constraints must be a list of natural-language strings')
    constraints = list(dict.fromkeys(
        _nonempty_str(c, 'budget.constraints entry') for c in raw_constraints))

    return {
        'limits': limits,
        'allowed': allowed,
        'constraints': constraints,
    }


def parse_goal_spec(spec, *, caller_goal_id: str = 'root') -> dict:
    """Parse and validate a structured /goal contract (dict or YAML string)."""
    if isinstance(spec, str):
        import yaml
        loaded = yaml.safe_load(spec)
        if not isinstance(loaded, dict):
            raise ValueError('goal YAML must decode to an object')
        data = dict(loaded)
    elif isinstance(spec, dict):
        data = dict(spec)
    else:
        raise ValueError('goal specification must be an object or YAML string')

    goal_id = data.get('goal_id')
    if goal_id is None or (isinstance(goal_id, str) and not goal_id.strip()):
        goal_id = 'g_' + uuid4().hex[:10]
    else:
        goal_id = _nonempty_str(goal_id, 'goal_id')
        if goal_id == 'root':
            raise ValueError('goal_id cannot be reserved name "root"')

    parent_goal_id = data.get('parent_goal_id') or caller_goal_id or 'root'
    parent_goal_id = _nonempty_str(parent_goal_id, 'parent_goal_id')
    if caller_goal_id and parent_goal_id != caller_goal_id:
        raise PermissionError(
            f'caller {caller_goal_id!r} cannot forge parent_goal_id {parent_goal_id!r}')

    req_ref = data.get('request_ref')
    if isinstance(req_ref, dict):
        request_id = _nonempty_str(req_ref.get('id'), 'request_ref.id')
        generation = req_ref.get('generation')
    else:
        request_id = _nonempty_str(data.get('request_id'), 'request_ref.id')
        generation = data.get('generation')
    if type(generation) is not int or generation < 1:
        raise ValueError('request_ref.generation must be a positive integer')

    objective = _nonempty_str(data.get('objective'), 'objective')
    done_criteria = _nonempty_str(data.get('done_criteria'), 'done_criteria')

    raw_scope = data.get('scope')
    if raw_scope is None:
        raw_scope = {}
    if not isinstance(raw_scope, dict):
        raise ValueError('scope must be an object')
    readable = raw_scope.get('readable_requests')
    if not isinstance(readable, list) or not readable:
        readable = [request_id]
    readable = list(dict.fromkeys(_nonempty_str(r, 'scope.readable_requests') for r in readable))
    if request_id not in readable:
        raise ValueError('scope.readable_requests must include request_ref.id')

    scope_paths = raw_scope.get('writable_paths')
    if scope_paths is not None and not isinstance(scope_paths, list):
        raise ValueError('scope.writable_paths must be a list of paths')

    can_ask_human = raw_scope.get('can_ask_human', False)
    if type(can_ask_human) is not bool:
        raise ValueError('scope.can_ask_human must be a boolean')
    if can_ask_human:
        raise AttenuationError('non-root goals must have scope.can_ask_human = false')

    budget = _parse_budget(data.get('budget'), extra_writable_paths=scope_paths)
    category = normalize_category(data.get('category'))

    return {
        'goal_id': goal_id,
        'parent_goal_id': parent_goal_id,
        'request_ref': {'id': request_id, 'generation': generation},
        'objective': objective,
        'done_criteria': done_criteria,
        'scope': {
            'readable_requests': readable,
            'can_ask_human': False,
        },
        'budget': budget,
        'category': category,
    }


def check_category_allowed(goal: dict) -> None:
    """Enforce ``budget.allowed.category`` (inherited monotonically like any allowlist).

    Omitting ``category`` means the ancor-exec default model and is always allowed.
    """
    category = goal.get('category')
    allowed = (goal.get('budget') or {}).get('allowed') or {}
    if category is not None and 'category' in allowed and category not in allowed['category']:
        raise AttenuationError(
            f"category {category!r} is not in budget.allowed.category {allowed['category']}")


def validate_and_inherit_attenuation(parent: dict, child: dict, active_siblings: list[dict],
                                     *, now: float | None = None) -> dict:
    """Enforce generic monotonic attenuation (limits, allowed, constraints) and inherit parent bounds."""
    if child['scope'].get('can_ask_human'):
        raise AttenuationError('child goal cannot set can_ask_human=true')

    parent_readable = set(parent['scope']['readable_requests'])
    child_readable = set(child['scope']['readable_requests'])
    if not child_readable <= parent_readable:
        extra = sorted(child_readable - parent_readable)
        raise AttenuationError(
            f"child readable_requests {extra} exceeds parent scope {sorted(parent_readable)}")

    p_budget = parent['budget']
    c_budget = child['budget']

    # 1. Generic numeric limits attenuation
    c_limits = dict(c_budget['limits'])
    for key, p_cap in p_budget['limits'].items():
        if _is_non_additive_limit(key):
            if key in c_limits:
                if float(c_limits[key]) > float(p_cap):
                    raise AttenuationError(
                        f"child limit {key!r} ({c_limits[key]}) exceeds parent limit ({p_cap})")
                if key == 'wall_hours' and now is not None and parent.get('deadline_at') is not None:
                    rem_h = max(0.0, (float(parent['deadline_at']) - float(now)) / 3600.0)
                    if float(c_limits[key]) > rem_h + 1e-6:
                        raise AttenuationError(
                            f"child wall_hours ({c_limits[key]}) exceeds parent remaining wall time ({rem_h:.2f}h)")
            else:
                # Inherit parent's non-additive cap so child cannot bypass it
                c_limits[key] = p_cap
        else:
            # Additive numeric resource: sum(active siblings) + child <= parent_cap
            # If child omits a parent-limited additive key, its allocation is 0 (cannot bypass parent).
            c_val = float(c_limits.get(key, 0))
            if key not in c_limits:
                c_limits[key] = 0
            sibling_sum = sum(float(s['budget']['limits'].get(key, 0)) for s in active_siblings)
            if sibling_sum + c_val > float(p_cap) + 1e-9:
                raise AttenuationError(
                    f"child limit {key!r} ({c_val:g}) plus active siblings ({sibling_sum:g}) "
                    f"exceeds parent limit ({p_cap:g})")

    # 2. Generic discrete allowlists attenuation
    c_allowed = dict(c_budget['allowed'])
    for key, p_vals in p_budget['allowed'].items():
        if key not in c_allowed:
            # Inherit parent's allowlist unchanged
            c_allowed[key] = list(p_vals)
            continue
        c_vals = c_allowed[key]
        if _is_path_key(key):
            for path in c_vals:
                if not _path_within(path, p_vals):
                    raise AttenuationError(
                        f"child allowed {key!r} path {path!r} is outside parent {p_vals}")
        else:
            extra = sorted(set(c_vals) - set(p_vals))
            if extra:
                raise AttenuationError(
                    f"child allowed {key!r} {extra} exceeds parent allowlist {p_vals}")

    # 3. Natural-language soft constraints: auto-inherit all parent constraints + append child's
    merged_constraints = list(dict.fromkeys([
        *p_budget.get('constraints', []),
        *c_budget.get('constraints', []),
    ]))

    return {
        **child,
        'budget': {
            'limits': c_limits,
            'allowed': c_allowed,
            'constraints': merged_constraints,
        },
    }


def validate_goal_result(result: dict, *, expected_goal_id: str | None = None) -> dict:
    """Validate structured GoalResult receipt (child -> parent)."""
    if not isinstance(result, dict):
        raise ValueError('GoalResult must be an object')
    goal_id = _nonempty_str(result.get('goal_id') or expected_goal_id, 'goal_id')
    if expected_goal_id and goal_id != expected_goal_id:
        raise ValueError(f'GoalResult goal_id {goal_id!r} does not match {expected_goal_id!r}')
    generation = result.get('request_generation')
    if type(generation) is not int or generation < 1:
        raise ValueError('request_generation must be a positive integer')
    status = result.get('status')
    if status not in TERMINAL_GOAL_STATUSES:
        raise ValueError(f'status must be one of {sorted(TERMINAL_GOAL_STATUSES)}')
    summary = _nonempty_str(result.get('summary'), 'summary')

    raw_artifacts = result.get('artifacts', [])
    if not isinstance(raw_artifacts, list):
        raise ValueError('artifacts must be a list')
    artifacts = []
    for item in raw_artifacts:
        if not isinstance(item, dict):
            raise ValueError('each artifact must be an object with path and desc')
        artifacts.append({
            'path': _nonempty_str(item.get('path'), 'artifact.path'),
            'desc': _nonempty_str(item.get('desc'), 'artifact.desc'),
        })

    metrics = result.get('metrics', {})
    if not isinstance(metrics, dict):
        raise ValueError('metrics must be an object')

    open_issues = result.get('open_issues', [])
    if not isinstance(open_issues, list) or any(not isinstance(i, str) or not i.strip() for i in open_issues):
        raise ValueError('open_issues must be a list of non-empty strings')
    if status in {'blocked', 'infeasible'} and not open_issues:
        raise ValueError(f'status={status!r} requires at least one entry in open_issues')

    return {
        'goal_id': goal_id,
        'request_generation': generation,
        'status': status,
        'summary': summary,
        'artifacts': artifacts,
        'metrics': metrics,
        'open_issues': open_issues,
    }


def _decode_goal(row) -> dict:
    item = dict(row)
    item['scope'] = json.loads(item['scope'])
    item['budget'] = json.loads(item['budget'])
    item['external_job_ids'] = json.loads(item['external_job_ids'])
    item['pending_signals'] = json.loads(item['pending_signals'])
    item['result'] = json.loads(item['result_json']) if item.get('result_json') else None
    item['needs_wake'] = bool(item['needs_wake'])
    item['observed_models'] = json.loads(item.get('observed_models') or '[]')
    item['request_ref'] = {'id': item['request_id'], 'generation': item['generation']}
    return item


class GoalStore:
    """Thin deterministic substrate over active_goals inside requirements.db."""

    def __init__(self, store, *, clock=time.time, global_limits: dict[str, float | int] | None = None,
                 category_router=None):
        self.store = store
        self.clock = clock
        self.global_limits = dict(global_limits or {})
        self.category_router = category_router

    def _validate_category(self, category: str | None) -> str | None:
        if category is None:
            return None
        if self.category_router is None:
            raise CategoryError(
                f'category {category!r} requested but no model routing is configured; '
                'omit category to use the ancor-exec default model')
        return self.category_router.validate(category)

    def _get(self, db, goal_id: str) -> dict:
        row = db.execute('SELECT * FROM active_goals WHERE goal_id=?', (goal_id,)).fetchone()
        if row is None:
            raise KeyError(goal_id)
        return _decode_goal(row)

    def _children(self, db, parent_goal_id: str) -> list[dict]:
        rows = db.execute(
            'SELECT * FROM active_goals WHERE parent_goal_id=? ORDER BY created_at, goal_id',
            (parent_goal_id,)).fetchall()
        return [_decode_goal(r) for r in rows]

    def _descendants(self, db, goal_id: str) -> list[dict]:
        result = []
        queue = [goal_id]
        seen = {goal_id}
        while queue:
            current = queue.pop(0)
            for child in self._children(db, current):
                cid = child['goal_id']
                if cid not in seen:
                    seen.add(cid)
                    result.append(child)
                    queue.append(cid)
        return result

    def _ancestors(self, db, goal_id: str) -> list[dict]:
        chain = []
        current = self._get(db, goal_id)
        seen = {goal_id}
        while current['parent_goal_id'] != 'root':
            pid = current['parent_goal_id']
            if pid in seen:
                break
            seen.add(pid)
            parent = self._get(db, pid)
            chain.append(parent)
            current = parent
        return chain

    def _is_ancestor(self, db, ancestor_id: str, target_id: str) -> bool:
        if ancestor_id == 'root':
            return True
        if ancestor_id == target_id:
            return False
        return any(node['goal_id'] == ancestor_id for node in self._ancestors(db, target_id))

    def _require_authority(self, db, caller_goal_id: str, target_id: str, *, allow_self: bool = False) -> None:
        if caller_goal_id == 'root':
            return
        if allow_self and caller_goal_id == target_id:
            return
        if not self._is_ancestor(db, caller_goal_id, target_id):
            raise PermissionError(
                f'caller {caller_goal_id!r} is not root or an ancestor of goal {target_id!r}')

    def get_goal(self, goal_id: str) -> dict:
        with self.store._transaction() as db:
            return self._get(db, goal_id)

    def list_goals(self, *, request_id: str | None = None, parent_goal_id: str | None = None,
                   status: str | None = None, include_terminal: bool = False) -> list[dict]:
        clauses = []
        params = []
        if request_id is not None:
            clauses.append('request_id=?')
            params.append(request_id)
        if parent_goal_id is not None:
            clauses.append('parent_goal_id=?')
            params.append(parent_goal_id)
        if status is not None:
            clauses.append('status=?')
            params.append(status)
        elif not include_terminal:
            placeholders = ','.join('?' for _ in ACTIVE_GOAL_STATUSES)
            clauses.append(f'status IN ({placeholders})')
            params.extend(sorted(ACTIVE_GOAL_STATUSES))
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self.store._transaction() as db:
            rows = db.execute(
                f'SELECT * FROM active_goals{where} ORDER BY created_at, goal_id',
                params).fetchall()
            return [_decode_goal(r) for r in rows]

    def subtree(self, goal_id: str) -> list[dict]:
        with self.store._transaction() as db:
            root = self._get(db, goal_id)
            return [root, *self._descendants(db, goal_id)]

    def spawn_goal(self, spec, *, caller_goal_id: str = 'root', session_id: str | None = None) -> dict:
        """Persist a new /goal after verifying request state and generic monotonic attenuation."""
        parsed = parse_goal_spec(spec, caller_goal_id=caller_goal_id)
        parsed['category'] = self._validate_category(parsed['category'])
        now = float(self.clock())
        request_id = parsed['request_ref']['id']
        generation = parsed['request_ref']['generation']
        parent_id = parsed['parent_goal_id']

        with self.store._transaction() as db:
            req = self.store._get(db, 'requests', request_id)
            if req['status'] != 'active' or req.get('target_status') is not None:
                raise ValueError(f'request {request_id} is not stably active')
            if req['generation'] != generation:
                raise RuntimeError(
                    f"goal generation {generation} does not match current request generation {req['generation']}")
            if db.execute('SELECT 1 FROM active_goals WHERE goal_id=?', (parsed['goal_id'],)).fetchone():
                raise ValueError(f"goal_id {parsed['goal_id']!r} already exists")

            if parent_id != 'root':
                parent = self._get(db, parent_id)
                if parent['status'] not in {'running', 'waiting_external'}:
                    raise RuntimeError(
                        f"parent goal {parent_id} is {parent['status']}; cannot spawn child goal")
                siblings = [
                    child for child in self._children(db, parent_id)
                    if child['status'] in NON_TERMINAL_BUDGET_STATUSES
                ]
                parsed = validate_and_inherit_attenuation(parent, parsed, siblings, now=now)
                wall_hours = parsed['budget']['limits'].get('wall_hours')
                deadline = (now + float(wall_hours) * 3600.0) if wall_hours is not None else None
                parent_deadline = parent.get('deadline_at')
                if parent_deadline is not None:
                    deadline = min(deadline, float(parent_deadline)) if deadline is not None else float(parent_deadline)
            else:
                if self.global_limits:
                    active_l1 = [
                        g for g in self._children(db, 'root')
                        if g['status'] in NON_TERMINAL_BUDGET_STATUSES
                    ]
                    for key, cap in self.global_limits.items():
                        if _is_non_additive_limit(key):
                            continue
                        req_val = float(parsed['budget']['limits'].get(key, 0))
                        allocated = sum(float(g['budget']['limits'].get(key, 0)) for g in active_l1)
                        if allocated + req_val > float(cap) + 1e-9:
                            raise AttenuationError(
                                f"L1 allocation for {key!r} ({req_val:g}) plus existing L1s "
                                f"({allocated:g}) exceeds global limit ({cap:g})")
                wall_hours = parsed['budget']['limits'].get('wall_hours')
                deadline = (now + float(wall_hours) * 3600.0) if wall_hours is not None else None

            check_category_allowed(parsed)
            sid = session_id or f"goal-{parsed['goal_id']}"
            db.execute(
                'INSERT INTO active_goals('
                'goal_id, parent_goal_id, request_id, generation, objective, done_criteria, '
                'scope, budget, session_id, pid, external_job_ids, status, paused_by_ancestor, '
                'needs_wake, pending_signals, result_json, created_at, updated_at, deadline_at, category) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, 1, ?, NULL, ?, ?, ?, ?)',
                (
                    parsed['goal_id'], parent_id, request_id, generation,
                    parsed['objective'], parsed['done_criteria'],
                    json.dumps(parsed['scope'], ensure_ascii=False),
                    json.dumps(parsed['budget'], ensure_ascii=False),
                    sid, '[]', 'running', '[]', now, now, deadline, parsed['category'],
                ))
            if parent_id == 'root' and req.get('observed_generation', 0) < generation:
                db.execute('UPDATE requests SET observed_generation=? WHERE id=?',
                           (generation, request_id))
            return self._get(db, parsed['goal_id'])

    def record_observed_model(self, goal_id: str, model: str, provider: str | None = None) -> bool:
        """Append one actually-used (model, provider) pair, observed by Hermes' post_api_request hook."""
        model = (model or '').strip()
        if not model:
            return False
        entry = {'model': model, 'provider': (provider or '').strip() or None}
        with self.store._transaction() as db:
            goal = self._get(db, goal_id)
            seen = list(goal['observed_models'])
            if entry in seen:
                return False
            seen.append(entry)
            db.execute('UPDATE active_goals SET observed_models=? WHERE goal_id=?',
                       (json.dumps(seen, ensure_ascii=False), goal_id))
            return True

    def read_request_for_goal(self, goal_id: str, request_id: str) -> dict:
        """Direct authoritative DB read for a non-root goal, enforcing scope.readable_requests."""
        with self.store._transaction() as db:
            goal = self._get(db, goal_id)
            if request_id not in goal['scope']['readable_requests']:
                raise PermissionError(
                    f'goal {goal_id} is not authorized to read request {request_id}')
            req = self.store._get(db, 'requests', request_id)
            points = self.store._points(db, request_id)
            grants = [dict(r) for r in db.execute(
                'SELECT id, action, target, scope FROM grants '
                'WHERE request_id=? AND revoked_change_id IS NULL ORDER BY created_at, id',
                (request_id,))]
            from .request_contract import FIELDS
            return {
                'request_id': req['id'],
                'title': req['title'],
                'status': req['status'],
                'generation': req['generation'],
                'content_mode': req['content_mode'],
                'fields': {
                    field: [{'point_id': p['point_id'], 'version_id': p['version_id'], 'body': p['body']}
                            for p in points if p['field'] == field]
                    for field in FIELDS
                },
                **({'brief': req['brief']} if req['content_mode'] != 'points' else {}),
                'grants': grants,
            }

    def signal_goal(self, goal_id: str, kind: str, payload: dict, *, caller_goal_id: str = 'root') -> dict:
        """Send a control signal (steer / update_generation) down the tree."""
        if kind not in {'steer', 'update_generation', 'child_result', 'watcher_event', 'recovery'}:
            raise ValueError(f'unsupported signal kind {kind!r}')
        now = float(self.clock())
        with self.store._transaction() as db:
            goal = self._get(db, goal_id)
            if kind in {'steer', 'update_generation'}:
                self._require_authority(db, caller_goal_id, goal_id)
            if goal['status'] in TERMINAL_GOAL_STATUSES:
                raise RuntimeError(f"cannot signal terminal goal {goal_id} ({goal['status']})")
            signals = list(goal['pending_signals'])
            signal_entry = {'kind': kind, 'payload': dict(payload), 'at': now}
            signals.append(signal_entry)
            new_gen = goal['generation']
            if kind == 'update_generation':
                req = self.store._get(db, 'requests', goal['request_id'])
                requested_gen = payload.get('generation', req['generation'])
                if type(requested_gen) is not int or requested_gen < 1:
                    raise ValueError('update_generation requires positive integer generation')
                new_gen = requested_gen
                signal_entry['payload']['generation'] = new_gen
                signal_entry['payload'].setdefault(
                    'instruction',
                    'Re-read authoritative requirement text directly from DB via ancor_request_read.')
                if caller_goal_id == 'root':
                    db.execute('UPDATE requests SET observed_generation=? WHERE id=?',
                               (new_gen, goal['request_id']))
            needs_wake = 0 if goal['status'] == 'paused' else 1
            new_status = 'running' if (goal['status'] == 'waiting_external' and needs_wake) else goal['status']
            db.execute(
                'UPDATE active_goals SET generation=?, pending_signals=?, needs_wake=?, '
                'status=?, updated_at=? WHERE goal_id=?',
                (new_gen, json.dumps(signals, ensure_ascii=False), needs_wake, new_status, now, goal_id))
            return self._get(db, goal_id)

    def report_result(self, goal_id: str, result: dict) -> dict:
        """Record a structured GoalResult and notify parent (Root via events, or parent goal via signal)."""
        validated = validate_goal_result(result, expected_goal_id=goal_id)
        now = float(self.clock())
        with self.store._transaction() as db:
            goal = self._get(db, goal_id)
            if goal['status'] in TERMINAL_GOAL_STATUSES:
                raise RuntimeError(f"goal {goal_id} is already terminal ({goal['status']})")
            # Execution metadata comes from Hermes hooks, never from the reporting agent.
            validated.update(category=goal.get('category'),
                             observed_models=goal.get('observed_models') or [])
            db.execute(
                'UPDATE active_goals SET status=?, pid=NULL, paused_by_ancestor=NULL, '
                'needs_wake=0, result_json=?, updated_at=? WHERE goal_id=?',
                (validated['status'], json.dumps(validated, ensure_ascii=False), now, goal_id))
            parent_id = goal['parent_goal_id']
            if parent_id == 'root':
                self.store._add_event(
                    db,
                    f"goal.{validated['status']}",
                    {
                        'goal_id': goal_id,
                        'request_id': goal['request_id'],
                        'generation': validated['request_generation'],
                        'status': validated['status'],
                        'summary': validated['summary'],
                        'artifacts': validated['artifacts'],
                        'metrics': validated['metrics'],
                        'open_issues': validated['open_issues'],
                        'category': goal.get('category'),
                        'observed_models': goal.get('observed_models') or [],
                    },
                    source_key=f"goal.result:{goal_id}:{validated['status']}",
                )
            else:
                parent = self._get(db, parent_id)
                if parent['status'] not in TERMINAL_GOAL_STATUSES:
                    signals = list(parent['pending_signals'])
                    signals.append({
                        'kind': 'child_result',
                        'payload': {**validated, 'category': goal.get('category'),
                                    'observed_models': goal.get('observed_models') or []},
                        'at': now,
                    })
                    needs_wake = 0 if parent['status'] == 'paused' else 1
                    new_status = ('running' if (parent['status'] == 'waiting_external' and needs_wake)
                                  else parent['status'])
                    db.execute(
                        'UPDATE active_goals SET pending_signals=?, needs_wake=?, status=?, updated_at=? '
                        'WHERE goal_id=?',
                        (json.dumps(signals, ensure_ascii=False), needs_wake, new_status, now, parent_id))
            return self._get(db, goal_id)

    def pause_goal(self, goal_id: str, *, mode: str = 'drain', caller_goal_id: str = 'root',
                   process_killer=default_process_killer, job_canceller=None) -> dict:
        """Deterministically pause target and cascade down the entire subtree."""
        if mode not in {'drain', 'preempt'}:
            raise ValueError('pause mode must be "drain" or "preempt"')
        now = float(self.clock())
        pids_to_kill: list[int] = []
        jobs_to_cancel: list[tuple[str, dict]] = []
        paused_ids: list[str] = []

        with self.store._transaction() as db:
            self._require_authority(db, caller_goal_id, goal_id)
            target = self._get(db, goal_id)
            if target['status'] in TERMINAL_GOAL_STATUSES:
                raise RuntimeError(f"cannot pause terminal goal {goal_id} ({target['status']})")

            nodes = [target, *self._descendants(db, goal_id)]
            for node in nodes:
                nid = node['goal_id']
                if node['status'] in TERMINAL_GOAL_STATUSES:
                    continue
                if node.get('pid'):
                    pids_to_kill.append(int(node['pid']))
                if mode == 'preempt':
                    for handle in node['external_job_ids']:
                        if handle.get('status', 'running') in ACTIVE_HANDLE_STATUSES:
                            jobs_to_cancel.append((nid, dict(handle)))

                if nid == goal_id:
                    ancestor_marker = node['paused_by_ancestor']
                    db.execute(
                        "UPDATE active_goals SET status='paused', paused_by_ancestor=?, "
                        'pid=NULL, needs_wake=0, updated_at=? WHERE goal_id=?',
                        (ancestor_marker, now, nid))
                    paused_ids.append(nid)
                elif node['status'] != 'paused':
                    db.execute(
                        "UPDATE active_goals SET status='paused', paused_by_ancestor=?, "
                        'pid=NULL, needs_wake=0, updated_at=? WHERE goal_id=?',
                        (goal_id, now, nid))
                    paused_ids.append(nid)

        killed = [pid for pid in pids_to_kill if process_killer and process_killer(pid)]
        cancelled_jobs = []
        if mode == 'preempt' and jobs_to_cancel:
            for nid, handle in jobs_to_cancel:
                if job_canceller is not None:
                    job_canceller(handle)
                cancelled_jobs.append(handle['handle_id'])
            with self.store._transaction() as db:
                for nid, handle in jobs_to_cancel:
                    node = self._get(db, nid)
                    updated = []
                    for existing in node['external_job_ids']:
                        if existing['handle_id'] == handle['handle_id']:
                            updated.append({**existing, 'status': 'cancelled', 'cancelled_at': now})
                        else:
                            updated.append(existing)
                    db.execute(
                        'UPDATE active_goals SET external_job_ids=?, updated_at=? WHERE goal_id=?',
                        (json.dumps(updated, ensure_ascii=False), now, nid))

        return {
            'goal_id': goal_id,
            'mode': mode,
            'paused_goals': paused_ids,
            'killed_pids': killed,
            'cancelled_jobs': cancelled_jobs,
        }

    def resume_goal(self, goal_id: str, *, caller_goal_id: str = 'root') -> dict:
        """Deterministically resume target and descendants paused_by_ancestor == goal_id."""
        now = float(self.clock())
        resumed_ids: list[str] = []
        with self.store._transaction() as db:
            self._require_authority(db, caller_goal_id, goal_id)
            target = self._get(db, goal_id)
            if target['status'] != 'paused':
                raise RuntimeError(f"goal {goal_id} is {target['status']}, not paused")
            if target['paused_by_ancestor'] is not None:
                ancestor = self._get(db, target['paused_by_ancestor'])
                if ancestor['status'] == 'paused':
                    raise RuntimeError(
                        f"cannot resume {goal_id} while ancestor {ancestor['goal_id']} is still paused")

            nodes = [target, *self._descendants(db, goal_id)]
            for node in nodes:
                nid = node['goal_id']
                if node['status'] != 'paused':
                    continue
                if nid != goal_id and node['paused_by_ancestor'] != goal_id:
                    continue
                has_active = any(
                    h.get('status', 'running') in ACTIVE_HANDLE_STATUSES
                    for h in node['external_job_ids']
                )
                has_signals = bool(node['pending_signals'])
                if has_active and not has_signals:
                    new_status, needs_wake = 'waiting_external', 0
                else:
                    new_status, needs_wake = 'running', 1
                db.execute(
                    'UPDATE active_goals SET status=?, paused_by_ancestor=NULL, '
                    'needs_wake=?, updated_at=? WHERE goal_id=?',
                    (new_status, needs_wake, now, nid))
                resumed_ids.append(nid)
        return {'goal_id': goal_id, 'resumed_goals': resumed_ids}

    def stop_goal(self, goal_id: str, *, reason: str = 'stopped', caller_goal_id: str = 'root',
                  process_killer=default_process_killer, job_canceller=None) -> dict:
        """Deterministically cancel target and entire subtree: kill PIDs and cancel external handles."""
        now = float(self.clock())
        pids_to_kill: list[int] = []
        jobs_to_cancel: list[tuple[str, dict]] = []
        cancelled_ids: list[str] = []

        with self.store._transaction() as db:
            self._require_authority(db, caller_goal_id, goal_id)
            target = self._get(db, goal_id)
            nodes = [target, *self._descendants(db, goal_id)]
            for node in nodes:
                nid = node['goal_id']
                if node['status'] in TERMINAL_GOAL_STATUSES:
                    continue
                if node.get('pid'):
                    pids_to_kill.append(int(node['pid']))
                for handle in node['external_job_ids']:
                    if handle.get('status', 'running') in ACTIVE_HANDLE_STATUSES:
                        jobs_to_cancel.append((nid, dict(handle)))
                updated_handles = [
                    ({**h, 'status': 'cancelled', 'cancelled_at': now}
                     if h.get('status', 'running') in ACTIVE_HANDLE_STATUSES else h)
                    for h in node['external_job_ids']
                ]
                cancel_result = {
                    'goal_id': nid,
                    'request_generation': node['generation'],
                    'status': 'cancelled',
                    'summary': reason,
                    'artifacts': [],
                    'metrics': {},
                    'open_issues': [],
                }
                db.execute(
                    "UPDATE active_goals SET status='cancelled', paused_by_ancestor=NULL, "
                    'pid=NULL, needs_wake=0, external_job_ids=?, result_json=?, updated_at=? '
                    'WHERE goal_id=?',
                    (json.dumps(updated_handles, ensure_ascii=False),
                     json.dumps(cancel_result, ensure_ascii=False), now, nid))
                cancelled_ids.append(nid)

        killed = [pid for pid in pids_to_kill if process_killer and process_killer(pid)]
        cancelled_jobs = []
        for _nid, handle in jobs_to_cancel:
            if job_canceller is not None:
                job_canceller(handle)
            cancelled_jobs.append(handle['handle_id'])

        return {
            'goal_id': goal_id,
            'cancelled_goals': cancelled_ids,
            'killed_pids': killed,
            'cancelled_jobs': cancelled_jobs,
        }

    def _subtree_active_usage_db(self, db, goal_id: str) -> dict[str, float]:
        nodes = [self._get(db, goal_id), *self._descendants(db, goal_id)]
        totals: dict[str, float] = {}
        for node in nodes:
            for handle in node['external_job_ids']:
                if handle.get('status', 'running') in ACTIVE_HANDLE_STATUSES:
                    for key, amt in (handle.get('usage') or {}).items():
                        totals[key] = totals.get(key, 0.0) + float(amt)
        return totals

    def subtree_active_usage(self, goal_id: str) -> dict[str, float]:
        """Compute active resource usage map across goal_id and its entire subtree."""
        with self.store._transaction() as db:
            return self._subtree_active_usage_db(db, goal_id)

    def register_resource_usage(self, goal_id: str, *, handle_id: str,
                                usage: dict[str, float | int] | None = None,
                                tags: dict[str, str] | None = None,
                                metadata: dict | None = None) -> dict:
        """Atomically enforce generic limits/allowed along the ancestor chain and register handle."""
        handle_id = _nonempty_str(handle_id, 'handle_id')
        usage = dict(usage or {})
        tags = dict(tags or {})
        for k, v in usage.items():
            _nonempty_str(k, 'usage key')
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f'usage[{k!r}] must be a non-negative number')
        for k, v in tags.items():
            _nonempty_str(k, 'tags key')
            _nonempty_str(v, f'tags[{k!r}]')

        now = float(self.clock())
        with self.store._transaction() as db:
            goal = self._get(db, goal_id)
            if goal['status'] != 'running':
                raise QuotaExceededError(
                    f"goal {goal_id} is {goal['status']}; resource acquisition rejected")

            chain = [goal, *self._ancestors(db, goal_id)]
            for node in chain:
                if node['status'] in {'paused', 'cancelled'}:
                    raise QuotaExceededError(
                        f"ancestor goal {node['goal_id']} is {node['status']}; acquisition rejected")
                allowed = node['budget'].get('allowed') or {}
                for tag_key, tag_val in tags.items():
                    if tag_key in allowed:
                        if _is_path_key(tag_key):
                            if not _path_within(tag_val, allowed[tag_key]):
                                raise QuotaExceededError(
                                    f"tag {tag_key}={tag_val!r} outside allowed paths on {node['goal_id']}")
                        elif tag_val not in allowed[tag_key]:
                            raise QuotaExceededError(
                                f"tag {tag_key}={tag_val!r} not in allowed {allowed[tag_key]} on {node['goal_id']}")

                limits = node['budget'].get('limits') or {}
                subtree_usage = self._subtree_active_usage_db(db, node['goal_id'])
                for res_key, req_amt in usage.items():
                    if res_key in limits:
                        cap = float(limits[res_key])
                        current = float(subtree_usage.get(res_key, 0.0))
                        if current + float(req_amt) > cap + 1e-9:
                            raise QuotaExceededError(
                                f"resource limit exceeded for {res_key!r} on {node['goal_id']}: "
                                f"active={current:g} + requested={req_amt:g} > limit={cap:g}")

            handles = list(goal['external_job_ids'])
            if any(h['handle_id'] == handle_id for h in handles):
                raise ValueError(f'handle_id {handle_id!r} already registered on goal {goal_id}')
            entry = {
                'handle_id': handle_id,
                'job_id': handle_id,
                'usage': usage,
                'tags': tags,
                'status': 'running',
                'submitted_at': now,
                **({'metadata': metadata} if metadata else {}),
            }
            handles.append(entry)
            db.execute(
                'UPDATE active_goals SET external_job_ids=?, updated_at=? WHERE goal_id=?',
                (json.dumps(handles, ensure_ascii=False), now, goal_id))
            return entry

    def mark_waiting_external(self, goal_id: str) -> dict:
        now = float(self.clock())
        with self.store._transaction() as db:
            goal = self._get(db, goal_id)
            if goal['status'] != 'running':
                return goal
            db.execute(
                "UPDATE active_goals SET status='waiting_external', pid=NULL, "
                'needs_wake=0, updated_at=? WHERE goal_id=?',
                (now, goal_id))
            return self._get(db, goal_id)

    def set_process(self, goal_id: str, pid: int, session_id: str | None = None) -> dict:
        now = float(self.clock())
        with self.store._transaction() as db:
            goal = self._get(db, goal_id)
            sid = session_id or goal['session_id']
            db.execute(
                'UPDATE active_goals SET pid=?, session_id=?, needs_wake=0, updated_at=? WHERE goal_id=?',
                (int(pid), sid, now, goal_id))
            return self._get(db, goal_id)

    def clear_process(self, goal_id: str, *, session_id: str | None = None) -> dict:
        now = float(self.clock())
        with self.store._transaction() as db:
            goal = self._get(db, goal_id)
            sid = session_id or goal['session_id']
            has_active = any(
                h.get('status', 'running') in ACTIVE_HANDLE_STATUSES
                for h in goal['external_job_ids']
            )
            new_status = goal['status']
            if goal['status'] == 'running' and has_active and not goal['pending_signals']:
                new_status = 'waiting_external'
            db.execute(
                'UPDATE active_goals SET pid=NULL, session_id=?, status=?, updated_at=? WHERE goal_id=?',
                (sid, new_status, now, goal_id))
            return self._get(db, goal_id)

    def consume_signals(self, goal_id: str) -> list[dict]:
        now = float(self.clock())
        with self.store._transaction() as db:
            goal = self._get(db, goal_id)
            signals = list(goal['pending_signals'])
            if signals or goal['needs_wake']:
                db.execute(
                    "UPDATE active_goals SET pending_signals='[]', needs_wake=0, updated_at=? "
                    'WHERE goal_id=?',
                    (now, goal_id))
            return signals

    def goals_needing_wake(self) -> list[dict]:
        with self.store._transaction() as db:
            rows = db.execute(
                "SELECT * FROM active_goals WHERE status='running' AND needs_wake=1 "
                'AND pid IS NULL ORDER BY created_at, goal_id').fetchall()
            return [_decode_goal(r) for r in rows]

    def total_active_usage(self) -> dict:
        """Global active resource utilization view for Root Agent."""
        totals: dict[str, float] = {}
        by_goal: dict[str, dict[str, float]] = {}
        with self.store._transaction() as db:
            rows = db.execute('SELECT * FROM active_goals').fetchall()
            for row in rows:
                goal = _decode_goal(row)
                g_usage: dict[str, float] = {}
                for handle in goal['external_job_ids']:
                    if handle.get('status', 'running') in ACTIVE_HANDLE_STATUSES:
                        for key, amt in (handle.get('usage') or {}).items():
                            val = float(amt)
                            totals[key] = totals.get(key, 0.0) + val
                            g_usage[key] = g_usage.get(key, 0.0) + val
                if g_usage:
                    by_goal[goal['goal_id']] = g_usage
        return {
            'active_usage': totals,
            'global_limits': self.global_limits,
            'by_goal': by_goal,
        }
