"""后台任务状态与事件流存储。"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

import aiosqlite


ACTIVE_STATUSES = {
    "pending",
    "thinking",
    "planning",
    "waiting_confirm",
    "confirming",
    "executing",
    "running",
    "analyzing",
}

TERMINAL_STATUSES = {
    "completed",
    "success",
    "failed",
    "blocked",
    "timeout",
    "canceled",
    "dry_run",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


async def create_task(
    db: aiosqlite.Connection,
    session_id: str,
    channel: str,
    title: str = "",
    total_steps: int = 1,
    confirm_mode: str = "interactive",
) -> dict:
    task_id = _gen_id("task")
    now = _now_iso()
    await db.execute(
        """
        INSERT INTO agent_tasks (
            id, session_id, channel, status, title, current_step, total_steps,
            confirm_mode, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            session_id,
            channel,
            "pending",
            title.strip(),
            0,
            max(1, total_steps),
            confirm_mode,
            now,
            now,
        ),
    )
    await db.commit()
    return await get_task(db, task_id) or {"id": task_id}


async def get_task(db: aiosqlite.Connection, task_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def claim_task_confirmation(
    db: aiosqlite.Connection,
    task_id: str,
    session_id: str,
    channel: str,
) -> tuple[bool, dict | None]:
    """Atomically claim one pending confirmation within its session scope.

    The conditional update is the idempotency boundary: only one concurrent
    request can move a task out of ``waiting_confirm``. Callers can inspect the
    returned task to acknowledge a duplicate request with its current state.
    """
    now = _now_iso()
    cursor = await db.execute(
        """
        UPDATE agent_tasks
        SET status = ?, updated_at = ?
        WHERE id = ? AND session_id = ? AND channel = ? AND status = ?
        """,
        ("confirming", now, task_id, session_id, channel, "waiting_confirm"),
    )
    await db.commit()
    db.row_factory = aiosqlite.Row
    task_cursor = await db.execute(
        "SELECT * FROM agent_tasks WHERE id = ? AND session_id = ? AND channel = ?",
        (task_id, session_id, channel),
    )
    row = await task_cursor.fetchone()
    return cursor.rowcount == 1, dict(row) if row else None


async def get_session_tasks(
    db: aiosqlite.Connection,
    session_id: str,
    channel: str | None = None,
    include_completed: bool = False,
) -> list[dict]:
    sql = "SELECT * FROM agent_tasks WHERE session_id = ?"
    params: list = [session_id]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    if not include_completed:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        sql += f" AND status IN ({placeholders})"
        params.extend(sorted(ACTIVE_STATUSES))
    sql += " ORDER BY updated_at DESC"
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    return [dict(row) for row in await cursor.fetchall()]


async def get_active_tasks(
    db: aiosqlite.Connection,
    *,
    session_id: str | None = None,
    channel: str | None = None,
) -> list[dict]:
    """Return active tasks, optionally scoped to one session and channel."""
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    sql = f"SELECT * FROM agent_tasks WHERE status IN ({placeholders})"
    params: list = sorted(ACTIVE_STATUSES)
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    sql += " ORDER BY updated_at DESC"
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    return [dict(row) for row in await cursor.fetchall()]


async def reconcile_orphaned_tasks(
    db: aiosqlite.Connection,
    *,
    owned_task_ids: set[str] | None = None,
    session_id: str | None = None,
) -> list[str]:
    """Cancel active tasks that have no live or durable owner.

    A persisted command awaiting confirmation remains resumable after a process
    restart. Other active states require an in-memory owner supplied by the web
    runtime; without one they are interrupted work, not running work.
    """
    owned = owned_task_ids or set()
    reconciled: list[str] = []
    for task in await get_active_tasks(db, session_id=session_id):
        task_id = str(task.get("id") or "")
        if not task_id or task_id in owned:
            continue
        durable_command_confirmation = (
            task.get("status") == "waiting_confirm"
            and bool(task.get("pending_command"))
            and bool(task.get("pending_target"))
        )
        durable_file_confirmation = False
        if task.get("status") == "waiting_confirm":
            cursor = await db.execute(
                """
                SELECT 1 FROM session_file_transfers
                WHERE turn_id = ? AND session_id = ? AND status = 'waiting_confirm'
                LIMIT 1
                """,
                (task_id, str(task.get("session_id") or "")),
            )
            durable_file_confirmation = await cursor.fetchone() is not None
        if durable_command_confirmation or durable_file_confirmation:
            continue
        await update_task(db, task_id, status="canceled", completed=True)
        await add_task_event(
            db,
            task_id,
            str(task.get("session_id") or ""),
            str(task.get("channel") or "chat"),
            "turn_state",
            status="canceled",
            content="任务已中断",
            payload={
                "turn_id": task_id,
                "session_id": str(task.get("session_id") or ""),
                "channel": str(task.get("channel") or "chat"),
                "status": "canceled",
                "label": "任务已中断",
                "active": False,
                "reconciled": True,
            },
        )
        reconciled.append(task_id)
    return reconciled


async def update_task(
    db: aiosqlite.Connection,
    task_id: str,
    *,
    status: str | None = None,
    current_step: int | None = None,
    total_steps: int | None = None,
    pending_command: str | None = None,
    pending_target: str | None = None,
    confirm_mode: str | None = None,
    completed: bool = False,
) -> dict | None:
    assignments = ["updated_at = ?"]
    now = _now_iso()
    params: list = [now]
    if status is not None:
        assignments.append("status = ?")
        params.append(status)
    if current_step is not None:
        assignments.append("current_step = ?")
        params.append(current_step)
    if total_steps is not None:
        assignments.append("total_steps = ?")
        params.append(max(1, total_steps))
    if pending_command is not None:
        assignments.append("pending_command = ?")
        params.append(pending_command)
    if pending_target is not None:
        assignments.append("pending_target = ?")
        params.append(pending_target)
    if confirm_mode is not None:
        assignments.append("confirm_mode = ?")
        params.append(confirm_mode)
    if completed:
        assignments.append("completed_at = ?")
        params.append(now)
        assignments.append("pending_command = ?")
        params.append("")
        assignments.append("pending_target = ?")
        params.append("")
    params.append(task_id)
    await db.execute(
        f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE id = ?",
        params,
    )
    await db.commit()
    return await get_task(db, task_id)


async def add_task_event(
    db: aiosqlite.Connection,
    task_id: str,
    session_id: str,
    channel: str,
    event_type: str,
    *,
    status: str = "",
    step_index: int | None = None,
    content: str = "",
    payload: dict | None = None,
) -> dict:
    event_id = _gen_id("evt")
    now = _now_iso()
    await db.execute(
        """
        INSERT INTO agent_task_events (
            id, task_id, session_id, channel, type, status, step_index,
            content, payload, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            task_id,
            session_id,
            channel,
            event_type,
            status,
            step_index,
            content,
            json.dumps(payload or {}, ensure_ascii=False),
            now,
        ),
    )
    await db.commit()
    return {
        "id": event_id,
        "task_id": task_id,
        "session_id": session_id,
        "channel": channel,
        "type": event_type,
        "status": status,
        "step_index": step_index,
        "content": content,
        "payload": payload or {},
        "created_at": now,
    }


async def get_task_events(
    db: aiosqlite.Connection,
    task_id: str,
) -> list[dict]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM agent_task_events WHERE task_id = ? ORDER BY created_at ASC, rowid ASC",
        (task_id,),
    )
    events = []
    for row in await cursor.fetchall():
        item = dict(row)
        payload = item.get("payload")
        try:
            item["payload"] = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            item["payload"] = {}
        events.append(item)
    return events
