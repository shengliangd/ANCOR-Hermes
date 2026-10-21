"""Read-only ANCOR dashboard API.

The router is mounted by Hermes at ``/api/plugins/ancor``.  All SQLite
connections use the URI read-only mode and every route is GET-only: this
module intentionally has no mutation endpoint or write transaction.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

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
    return _rows(conn, "SELECT id,title,status,replacement_of FROM requirements ORDER BY rowid")


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
    requirements, links, events = [], [], []
    if conn is not None:
        try:
            requirements = _requirements(conn)
            links = _links(conn)
            events = _rows(conn, "SELECT id,kind,payload,source_key FROM events ORDER BY id")
        finally:
            conn.close()
    tasks = _read_tasks()
    by_req = {link["task_id"]: link["requirement_id"] for link in links}
    titles = {row["id"]: row["title"] for row in requirements}
    task_items = [task_ref(task, by_req.get(task.get("id")), titles.get(by_req.get(task.get("id")))) for task in tasks]
    notifications = _notifications()
    visible = notification_selection(notifications)
    return {
        "requirements": {"items": [requirement_ref(row) for row in requirements], "count": len(requirements), "returned": len(requirements), "truncated": False},
        "tasks": {"items": task_items, "count": len(task_items), "returned": len(task_items), "truncated": False},
        "questions": {"items": [notification_summary(row) for row in visible if row.get("kind") == "question"], "count": sum(row.get("kind") == "question" for row in notifications), "returned": sum(row.get("kind") == "question" for row in visible), "truncated": False},
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
        return {"requirement": requirement_detail(row), "task_ids": [item["task_id"] for item in links]}
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
