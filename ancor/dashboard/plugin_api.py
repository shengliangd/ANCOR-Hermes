"""Read-only ANCOR dashboard API.

The router is mounted by Hermes at ``/api/plugins/ancor``.  All SQLite
connections use the URI read-only mode and every route is GET-only: this
module intentionally has no mutation endpoint or write transaction.
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

# Hermes imports this file directly, outside the agent plugin's namespace.
# Resolve siblings under an installation-specific package without executing the
# plugin entrypoint or adding unrelated installed plugins to sys.path.
if not __package__:
    root = Path(__file__).resolve().parents[1]
    namespace = "_ancor_dashboard_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    if namespace not in sys.modules:
        package = types.ModuleType(namespace)
        package.__path__ = [str(root)]
        sys.modules[namespace] = package
    __package__ = namespace + ".dashboard"

from ..views import event_summary, notification_detail, notification_selection, notification_summary, request_detail, request_ref, task_detail, task_ref

router = APIRouter()


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    return Path(get_hermes_home()).expanduser()


def _settings() -> dict:
    # Read configuration without initializing Hermes or the request store.
    path = _home() / "config.yaml"
    if not path.exists():
        return {}
    try:
        import hermes_yaml as yaml
    except ImportError:
        import yaml
    config = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    return config.get("plugins", {}).get("entries", {}).get("ancor", {}).get("settings", {})


def _store_path() -> Path:
    raw = (os.environ.get("ANCOR_STORE_PATH") or os.environ.get("ANCOR_DB_PATH")
           or _settings().get("store_path"))
    return Path(raw).expanduser() if raw else _home() / "ancor" / "requests.db"


def _board():
    from ..board import NativeBoard
    settings = _settings()
    env = dict(os.environ, HERMES_HOME=str(_home()))
    return NativeBoard(
        os.environ.get("HERMES_KANBAN_BOARD") or settings.get("board", "ancor"),
        profile=settings.get("coordinator_profile", "ancor-coordinator"), env=env)


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


def _requests(conn):
    return _rows(conn, "SELECT * FROM requests ORDER BY rowid")


def _links(conn):
    return _rows(conn, "SELECT request_id,task_id FROM task_links ORDER BY request_id,task_id")


def _read_tasks():
    """Native board listing is a read-only CLI bridge; failures yield an empty board."""
    try:
        return _board().list()
    except Exception:
        return []


def _task_map(tasks):
    return {task.get("id"): task for task in tasks if task.get("id")}


def _notifications():
    path = _notifications_path()
    conn = _ro(path)
    if conn is None:
        return []
    try:
        rows = _rows(conn, "SELECT * FROM human_notifications ORDER BY rowid")
        # Only display fields cross into the browser; confirmation bindings and
        # transport receipts remain in the durable store.
        visible = {"id", "kind", "message", "state", "answer", "request_id", "task_id",
                   "queued_at", "delivery_started_at", "next_attempt_at", "closed_at",
                   "closed_by", "close_reason", "error"}
        return [{key: value for key, value in row.items() if key in visible} for row in rows]
    finally:
        conn.close()


@router.get("/overview")
def overview():
    conn = _ro(_store_path())
    requests, links, events, questions = [], [], [], []
    if conn is not None:
        try:
            requests = _requests(conn)
            links = _links(conn)
            events = _rows(conn, "SELECT id,kind,payload,source_key FROM events ORDER BY id")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='questions'").fetchone():
                questions = _rows(conn, "SELECT id,request_id,body,state,revision FROM questions ORDER BY created_at")
        finally:
            conn.close()
    tasks = _read_tasks()
    by_req = {link["task_id"]: link["request_id"] for link in links}
    titles = {row["id"]: row["title"] for row in requests}
    task_items = [task_ref(task, by_req.get(task.get("id")), titles.get(by_req.get(task.get("id")))) for task in tasks]
    notifications = _notifications()
    visible = notification_selection(notifications)
    question_ids = {q["id"] for q in questions}
    question_items = [{"id": q["id"], "title": q["body"][:200], "state": q["state"],
                       "detail_kind": "questions"} for q in questions if q["state"] == "open"]
    question_items.extend({**notification_summary(n), "detail_kind": "notifications"}
                          for n in visible if n["kind"] == "question" and n["id"] not in question_ids)
    return {
        "requests": {"items": [request_ref(row) for row in requests], "count": len(requests), "returned": len(requests), "truncated": False},
        "tasks": {"items": task_items, "count": len(task_items), "returned": len(task_items), "truncated": False},
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
        links = _rows(conn, "SELECT task_id FROM task_links WHERE request_id=? ORDER BY task_id", (request_id,))
        result = {"request": request_detail(row), "task_ids": [item["task_id"] for item in links]}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='point_versions'").fetchone():
            points = _rows(conn, "SELECT * FROM point_versions WHERE request_id=? AND invalidated_at IS NULL ORDER BY position,point_id", (request_id,))
            result["fields"] = {field: [p for p in points if p["field"] == field] for field in ("outcome", "constraint", "advice", "context")}
            result["questions"] = _rows(conn, "SELECT * FROM questions WHERE request_id=? ORDER BY created_at", (request_id,))
            result["coordination"] = _rows(conn, "SELECT * FROM impact_reviews WHERE request_id=? AND generation=? ORDER BY task_id", (request_id, row["generation"]))
            changes = _rows(conn, "SELECT id,actor,operations,created_at,committed_at FROM changes WHERE request_id=? AND committed_at IS NOT NULL AND json_extract(source,'$.kind') IS NOT 'coordinator_note' ORDER BY committed_at DESC LIMIT 50", (request_id,))
            for change in changes:
                change["operations"] = json.loads(change["operations"])
            total = conn.execute("SELECT COUNT(*) FROM changes WHERE request_id=? AND committed_at IS NOT NULL AND json_extract(source,'$.kind') IS NOT 'coordinator_note'", (request_id,)).fetchone()[0]
            result["changes"] = {"items": changes, "count": total, "returned": len(changes), "truncated": total > len(changes)}
        return _redact(result)
    finally:
        conn.close()


@router.get("/tasks/{task_id}")
def task(task_id: str):
    tasks = _task_map(_read_tasks())
    row = tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        detail = _board()._call("show", task_id)
    except Exception:
        detail = {"task": row, "comments": [], "events": [], "runs": [], "parents": [], "children": []}
    return {"task": task_detail(detail)}


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
