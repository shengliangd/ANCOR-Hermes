"""Read-only ANCOR dashboard API.

The router is mounted by Hermes at ``/api/plugins/ancor``.  All SQLite
connections use the URI read-only mode and every route is GET-only: this
module intentionally has no mutation endpoint or write transaction.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ancor.views import event_summary, notification_detail, notification_selection, notification_summary, requirement_detail, requirement_ref, task_detail, task_ref

router = APIRouter()


def _store_path() -> Path:
    raw = os.environ.get("ANCOR_STORE_PATH") or os.environ.get("ANCOR_DB_PATH")
    return Path(raw).expanduser() if raw else Path.home() / ".hermes" / "ancor" / "requirements.db"


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
    sensitive = {"token", "access_token", "confirmation_reference", "answer_reference", "receipt", "target", "cvc", "password"}
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


def _requirements(conn):
    return _rows(conn, "SELECT * FROM requirements ORDER BY rowid")


def _links(conn):
    return _rows(conn, "SELECT requirement_id,task_id FROM task_links ORDER BY requirement_id,task_id")


def _read_tasks():
    """Native board listing is a read-only CLI bridge; failures yield an empty board."""
    try:
        from ancor.board import NativeBoard
        return NativeBoard(os.environ.get("HERMES_KANBAN_BOARD", "ancor")).list()
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
        for row in rows:
            if row.get("receipt"):
                try:
                    row["receipt"] = json.loads(row["receipt"])
                except ValueError:
                    row["receipt"] = None
            # Never return receipt/target/transport identity to a browser.
            row.pop("receipt", None)
            row.pop("target", None)
        return rows
    finally:
        conn.close()


@router.get("/overview")
def overview():
    conn = _ro(_store_path())
    requirements, links, events, questions = [], [], [], []
    if conn is not None:
        try:
            requirements = _requirements(conn)
            links = _links(conn)
            events = _rows(conn, "SELECT id,kind,payload,source_key FROM events ORDER BY id")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='questions'").fetchone():
                questions = _rows(conn, "SELECT id,requirement_id,body,state,revision FROM questions ORDER BY created_at")
        finally:
            conn.close()
    tasks = _read_tasks()
    by_req = {link["task_id"]: link["requirement_id"] for link in links}
    titles = {row["id"]: row["title"] for row in requirements}
    task_items = [task_ref(task, by_req.get(task.get("id")), titles.get(by_req.get(task.get("id")))) for task in tasks]
    notifications = _notifications()
    visible = notification_selection(notifications)
    question_ids = {q["id"] for q in questions}
    question_items = [{"id": q["id"], "title": q["body"][:200], "state": q["state"],
                       "detail_kind": "questions"} for q in questions if q["state"] == "open"]
    question_items.extend({**notification_summary(n), "detail_kind": "notifications"}
                          for n in visible if n["kind"] == "question" and n["id"] not in question_ids)
    return {
        "requirements": {"items": [requirement_ref(row) for row in requirements], "count": len(requirements), "returned": len(requirements), "truncated": False},
        "tasks": {"items": task_items, "count": len(task_items), "returned": len(task_items), "truncated": False},
        "questions": {"items": question_items[:50], "count": len(question_items), "returned": min(50, len(question_items)), "truncated": len(question_items) > 50},
        "notifications": {"items": [notification_summary(row) for row in visible], "count": len(notifications), "returned": len(visible), "truncated": len(visible) < len(notifications)},
        "events": {"items": [_safe_event(row) for row in events[-50:]], "count": len(events), "returned": min(50, len(events)), "truncated": len(events) > 50},
        "poll_interval_seconds": 5,
    }


@router.get("/requirements/{requirement_id}")
def requirement(requirement_id: str):
    conn = _ro(_store_path())
    if conn is None:
        raise HTTPException(status_code=404, detail="requirement not found")
    try:
        row = _one(conn, "SELECT * FROM requirements WHERE id=?", (requirement_id,), "requirement")
        links = _rows(conn, "SELECT task_id FROM task_links WHERE requirement_id=? ORDER BY task_id", (requirement_id,))
        result = {"requirement": requirement_detail(row), "task_ids": [item["task_id"] for item in links]}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='point_versions'").fetchone():
            points = _rows(conn, "SELECT * FROM point_versions WHERE requirement_id=? AND invalidated_at IS NULL ORDER BY position,point_id", (requirement_id,))
            result["fields"] = {field: [p for p in points if p["field"] == field] for field in ("outcome", "constraint", "advice", "context")}
            result["questions"] = _rows(conn, "SELECT * FROM questions WHERE requirement_id=? ORDER BY created_at", (requirement_id,))
            result["coordination"] = _rows(conn, "SELECT * FROM impact_reviews WHERE requirement_id=? AND generation=? ORDER BY task_id", (requirement_id, row["generation"]))
            changes = _rows(conn, "SELECT id,actor,operations,created_at,committed_at FROM changes WHERE requirement_id=? AND committed_at IS NOT NULL AND json_extract(source,'$.kind') IS NOT 'coordinator_note' ORDER BY committed_at DESC LIMIT 50", (requirement_id,))
            for change in changes:
                change["operations"] = json.loads(change["operations"])
            total = conn.execute("SELECT COUNT(*) FROM changes WHERE requirement_id=? AND committed_at IS NOT NULL AND json_extract(source,'$.kind') IS NOT 'coordinator_note'", (requirement_id,)).fetchone()[0]
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
        from ancor.board import NativeBoard
        detail = NativeBoard(os.environ.get("HERMES_KANBAN_BOARD", "ancor"))._call("show", task_id)
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
