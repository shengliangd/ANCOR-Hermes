"""Read-only ANCOR dashboard API over requirements.db, active_goals, and judge_verdicts.

All SQLite connections use URI read-only mode and every route is GET-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import types
from pathlib import Path

from fastapi import APIRouter, HTTPException

if not __package__:
    root = Path(__file__).resolve().parents[1]
    namespace = "_ancor_dashboard_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    if namespace not in sys.modules:
        package = types.ModuleType(namespace)
        package.__path__ = [str(root)]
        sys.modules[namespace] = package
    __package__ = namespace + ".dashboard"

from ..request_contract import FIELDS, LABELS
from ..views import (event_summary, notification_detail, notification_selection,
                     notification_summary, request_detail, request_ref)

router = APIRouter()


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    return Path(get_hermes_home()).expanduser()


def _settings() -> dict:
    path = _home() / "config.yaml"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    try:
        import hermes_yaml as yaml
        config = yaml.safe_load(text) or {}
    except ImportError:
        try:
            import yaml
            config = yaml.safe_load(text) or {}
        except ImportError:
            from ruamel.yaml import YAML
            config = YAML(typ="safe").load(text) or {}
    return config.get("plugins", {}).get("entries", {}).get("ancor", {}).get("settings", {})


def _store_path() -> Path:
    raw = (os.environ.get("ANCOR_STORE_PATH") or os.environ.get("ANCOR_DB_PATH")
           or _settings().get("store_path"))
    return Path(raw).expanduser() if raw else _home() / "ancor" / "requests.db"


def _notifications_path() -> Path:
    raw = os.environ.get("ANCOR_NOTIFICATIONS_PATH")
    return Path(raw).expanduser() if raw else Path(str(_store_path()) + ".notifications.db")


def _ro(path: Path):
    if not path.exists():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql: str, args=()):
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def _one(conn, sql: str, args=(), label="item"):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return dict(row)


def _redact(value):
    sensitive = {"token", "access_token", "confirmation_reference", "answer_reference",
                 "reference", "session_id", "tool_call_id", "receipt", "target", "cvc", "password"}
    if isinstance(value, dict):
        return {key: "[redacted]" if key.lower() in sensitive else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _safe_event(row: dict) -> dict:
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    row["payload"] = _redact(payload)
    return event_summary(row)


def _decode_goal_row(row: dict) -> dict:
    out = dict(row)
    for field in ("scope", "budget", "external_job_ids", "pending_signals", "observed_models", "backend_args"):
        if isinstance(out.get(field), str):
            out[field] = json.loads(out[field])
    if isinstance(out.get("result_json"), str):
        out["result"] = json.loads(out["result_json"])
    out.pop("result_json", None)
    if isinstance(out.get("handoff_json"), str):
        out["handoff"] = json.loads(out["handoff_json"])
    else:
        out["handoff"] = None
    out.pop("handoff_json", None)
    if isinstance(out.get("checklist_json"), str):
        out["checklist"] = json.loads(out["checklist_json"])
    else:
        out["checklist"] = []
    out.pop("checklist_json", None)
    return _redact(out)


def _decode_shift_row(row: dict) -> dict:
    out = dict(row)
    for field in ("backend_args", "observed_models"):
        if isinstance(out.get(field), str):
            out[field] = json.loads(out[field])
    out["reused_session"] = bool(out.get("reused_session"))
    if isinstance(out.get("handoff_snapshot"), str):
        out["handoff_snapshot"] = json.loads(out["handoff_snapshot"])
    return _redact(out)


def _notifications():
    path = _notifications_path()
    conn = _ro(path)
    if conn is None:
        return []
    try:
        rows = _rows(conn, "SELECT * FROM human_notifications ORDER BY rowid")
        visible = {"id", "kind", "message", "state", "answer", "request_id",
                   "queued_at", "delivery_started_at", "next_attempt_at", "closed_at",
                   "closed_by", "close_reason", "error"}
        return [{key: value for key, value in row.items() if key in visible} for row in rows]
    finally:
        conn.close()


@router.get("/overview")
def overview():
    conn = _ro(_store_path())
    requests, goals, events, questions = [], [], [], []
    if conn is not None:
        try:
            requests = _rows(conn, "SELECT * FROM requests ORDER BY rowid")
            events = _rows(conn, "SELECT id,kind,payload,source_key FROM events ORDER BY id")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='questions'").fetchone():
                questions = _rows(conn, "SELECT id,request_id,body,state,revision FROM questions ORDER BY created_at")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='active_goals'").fetchone():
                goals = [_decode_goal_row(r) for r in _rows(conn, "SELECT * FROM active_goals ORDER BY created_at")]
        finally:
            conn.close()
    notifications = _notifications()
    visible = notification_selection(notifications)
    question_ids = {q["id"] for q in questions}
    question_items = [{"id": q["id"], "title": q["body"][:200], "state": q["state"],
                       "detail_kind": "questions"} for q in questions if q["state"] == "open"]
    question_items.extend({**notification_summary(n), "detail_kind": "notifications"}
                          for n in visible if n["kind"] == "question" and n["id"] not in question_ids)
    return {
        "requests": {"items": [request_ref(row) for row in requests], "count": len(requests), "returned": len(requests), "truncated": False},
        "goals": {"items": goals, "count": len(goals), "returned": len(goals), "truncated": False},
        "questions": {"items": question_items[:50], "count": len(question_items), "returned": min(50, len(question_items)), "truncated": len(question_items) > 50},
        "notifications": {"items": [notification_summary(row) for row in visible], "count": len(notifications), "returned": len(visible), "truncated": len(visible) < len(notifications)},
        "events": {"items": [_safe_event(row) for row in events[-50:]], "count": len(events), "returned": min(50, len(events)), "truncated": len(events) > 50},
        "poll_interval_seconds": 5,
    }


@router.get("/requests/{request_id}")
def request(request_id: str):
    conn = _ro(_store_path())
    if conn is None:
        raise HTTPException(status_code=404, detail="request not found")
    try:
        row = _one(conn, "SELECT * FROM requests WHERE id=?", (request_id,), "request")
        result = {"request": request_detail(row)}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='active_goals'").fetchone():
            result["goals"] = [
                _decode_goal_row(g)
                for g in _rows(conn, "SELECT * FROM active_goals WHERE request_id=? ORDER BY created_at", (request_id,))
            ]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='judge_verdicts'").fetchone():
            verdicts = _rows(conn, "SELECT * FROM judge_verdicts WHERE request_id=? ORDER BY id", (request_id,))
            for v in verdicts:
                v["feedback"] = json.loads(v["feedback"])
                v["artifacts"] = json.loads(v["artifacts"])
                v["evidence"] = json.loads(v["evidence"]) if v["evidence"] else None
                v["runtime"] = json.loads(v["runtime"]) if v.get("runtime") else []
            result["judge_verdicts"] = verdicts
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='point_versions'").fetchone():
            points = _rows(conn, "SELECT * FROM point_versions WHERE request_id=? AND invalidated_at IS NULL ORDER BY position,point_id", (request_id,))
            result["fields"] = {field: [p for p in points if p["field"] == field] for field in FIELDS}
            result["field_labels"] = LABELS
            result["questions"] = _rows(conn, "SELECT * FROM questions WHERE request_id=? ORDER BY created_at", (request_id,))
            changes = _rows(conn, "SELECT id,actor,operations,created_at,committed_at FROM changes WHERE request_id=? AND committed_at IS NOT NULL AND json_extract(source,'$.kind') IS NOT 'coordinator_note' ORDER BY committed_at DESC LIMIT 50", (request_id,))
            for change in changes:
                change["operations"] = json.loads(change["operations"])
            total = conn.execute("SELECT COUNT(*) FROM changes WHERE request_id=? AND committed_at IS NOT NULL AND json_extract(source,'$.kind') IS NOT 'coordinator_note'", (request_id,)).fetchone()[0]
            result["changes"] = {"items": changes, "count": total, "returned": len(changes), "truncated": total > len(changes)}
        return _redact(result)
    finally:
        conn.close()


@router.get("/goals/{goal_id}")
def goal(goal_id: str):
    conn = _ro(_store_path())
    if conn is None:
        raise HTTPException(status_code=404, detail="goal not found")
    try:
        row = _one(conn, "SELECT * FROM active_goals WHERE goal_id=?", (goal_id,), "goal")
        decoded = _decode_goal_row(row)
        shifts = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='goal_shifts'").fetchone():
            shifts = [
                _decode_shift_row(s)
                for s in _rows(conn, "SELECT * FROM goal_shifts WHERE goal_id=? ORDER BY shift_seq, id", (goal_id,))
            ]
        return {
            "goal": decoded,
            "handoff": decoded.get("handoff"),
            "checklist": decoded.get("checklist") or [],
            "shifts": shifts,
        }
    finally:
        conn.close()


@router.get("/notifications/{notification_id}")
def notification(notification_id: str):
    row = next((item for item in _notifications() if item.get("id") == notification_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="notification not found")
    return {"notification": notification_detail(row)}


@router.get("/events/{event_id}")
def event(event_id: int):
    conn = _ro(_store_path())
    if conn is None:
        raise HTTPException(status_code=404, detail="event not found")
    try:
        row = _one(conn, "SELECT id,kind,payload,source_key FROM events WHERE id=?", (event_id,), "event")
        payload = row.get("payload")
        if isinstance(payload, str):
            row["payload"] = json.loads(payload)
        row["payload"] = _redact(row["payload"])
        return {"event": {"id": row["id"], "kind": row["kind"], "payload": row["payload"], "source_key": row.get("source_key")}}
    finally:
        conn.close()


@router.get("/health")
def health():
    return {"ok": True, "readonly": True, "poll_interval_seconds": 5}


@router.get("/changes/{change_id}")
def change(change_id: str):
    conn = _ro(_store_path())
    if conn is None:
        raise HTTPException(status_code=404, detail="change not found")
    try:
        row = _one(conn, "SELECT * FROM changes WHERE id=? AND committed_at IS NOT NULL AND json_extract(source,'$.kind') IS NOT 'coordinator_note'", (change_id,), "change")
        for field in ("base", "operations", "source", "result"):
            row[field] = json.loads(row[field]) if row[field] is not None else None
        return _redact(row)
    finally:
        conn.close()


@router.get("/questions/{question_id}")
def question(question_id: str):
    conn = _ro(_store_path())
    if conn is None:
        raise HTTPException(status_code=404, detail="question not found")
    try:
        row = _one(conn, "SELECT * FROM questions WHERE id=?", (question_id,), "question")
        row["point_ids"] = json.loads(row["point_ids"])
        row["replies"] = _rows(conn, "SELECT * FROM question_replies WHERE question_id=? ORDER BY created_at", (question_id,))
        return _redact(row)
    finally:
        conn.close()
