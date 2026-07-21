"""会话与消息存储。"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

import aiosqlite


_AUTO_TITLE_PLACEHOLDERS = (
    "新聊天",
    "新会话",
    "新命令会话",
    "命令终端",
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


async def create_session(
    db: aiosqlite.Connection,
    session_type: str = "chat",
    title: str = "",
    caller: str = "web_user",
    source: str = "web",
) -> dict:
    session_id = _gen_id("sess")
    now = _now_iso()
    title = title.strip() or ("新聊天" if session_type == "chat" else "新命令会话")
    await db.execute(
        """
        INSERT INTO sessions (id, title, type, caller, source, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, title, session_type, caller, source, "active", now, now),
    )
    await db.commit()
    return {
        "id": session_id,
        "title": title,
        "type": session_type,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "pinned_at": None,
        "deleted_at": None,
    }


async def ensure_session(
    db: aiosqlite.Connection,
    session_id: str,
    session_type: str = "chat",
    title: str = "",
) -> None:
    if not session_id:
        return
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    if row:
        return
    now = _now_iso()
    await db.execute(
        """
        INSERT INTO sessions (id, title, type, caller, source, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            title.strip() or ("新聊天" if session_type == "chat" else "新命令会话"),
            session_type,
            "web_user",
            "web",
            "active",
            now,
            now,
        ),
    )
    await db.commit()


async def list_sessions(
    db: aiosqlite.Connection,
    session_type: str | None = None,
    query: str = "",
    limit: int = 100,
) -> list[dict]:
    sql = "SELECT * FROM sessions WHERE deleted_at IS NULL"
    params: list = []
    if session_type:
        sql += " AND type = ?"
        params.append(session_type)
    query = query.strip()
    if query:
        like = f"%{query}%"
        sql += """
            AND (
                title LIKE ?
                OR id IN (
                    SELECT session_id FROM session_messages
                    WHERE content LIKE ? OR payload LIKE ?
                )
            )
        """
        params.extend([like, like, like])
    sql += " ORDER BY (pinned_at IS NULL) ASC, updated_at DESC LIMIT ?"
    params.append(limit)
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def rename_session(
    db: aiosqlite.Connection,
    session_id: str,
    title: str,
) -> dict | None:
    title = _compact_title(title)
    if not title:
        return None
    now = _now_iso()
    cursor = await db.execute(
        """
        UPDATE sessions
        SET title = ?, updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
        """,
        (title, now, session_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    return await get_session(db, session_id)


async def set_session_pinned(
    db: aiosqlite.Connection,
    session_id: str,
    pinned: bool,
) -> dict | None:
    pinned_at = _now_iso() if pinned else None
    cursor = await db.execute(
        """
        UPDATE sessions
        SET pinned_at = ?
        WHERE id = ? AND deleted_at IS NULL
        """,
        (pinned_at, session_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM sessions WHERE id = ? AND deleted_at IS NULL",
        (session_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_session(
    db: aiosqlite.Connection,
    session_id: str,
    message_limit: int | None = None,
) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM sessions WHERE id = ? AND deleted_at IS NULL",
        (session_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    session = dict(row)
    total_messages = 0
    if message_limit and message_limit > 0:
        cursor = await db.execute(
            "SELECT COUNT(*) AS count FROM session_messages WHERE session_id = ?",
            (session_id,),
        )
        count_row = await cursor.fetchone()
        total_messages = int(count_row["count"] if count_row else 0)
        cursor = await db.execute(
            """
            SELECT * FROM (
                SELECT rowid AS _rowid, * FROM session_messages
                WHERE session_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
            ) ORDER BY created_at ASC, _rowid ASC
            """,
            (session_id, max(1, int(message_limit))),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        )
    messages = []
    for message in await cursor.fetchall():
        item = dict(message)
        payload = item.get("payload")
        if payload:
            try:
                item["payload"] = json.loads(payload)
            except json.JSONDecodeError:
                item["payload"] = {}
        else:
            item["payload"] = {}
        messages.append(item)
    session["messages"] = messages
    session["messages_truncated"] = max(0, total_messages - len(messages)) if total_messages else 0
    return session


async def soft_delete_session(db: aiosqlite.Connection, session_id: str) -> bool:
    now = _now_iso()
    cursor = await db.execute(
        """
        UPDATE sessions
        SET deleted_at = ?, status = ?, updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
        """,
        (now, "deleted", now, session_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def add_session_message(
    db: aiosqlite.Connection,
    session_id: str,
    role: str,
    msg_type: str,
    content: str = "",
    payload: dict | None = None,
) -> dict:
    now = _now_iso()
    message_id = _gen_id("msg")
    await db.execute(
        """
        INSERT INTO session_messages (id, session_id, role, type, content, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            session_id,
            role,
            msg_type,
            content,
            json.dumps(payload or {}, ensure_ascii=False),
            now,
        ),
    )
    await db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (now, session_id),
    )
    await db.commit()
    return {
        "id": message_id,
        "session_id": session_id,
        "role": role,
        "type": msg_type,
        "content": content,
        "payload": payload or {},
        "created_at": now,
    }


async def update_session_context_summary(
    db: aiosqlite.Connection,
    session_id: str,
    summary: str,
    covered_message_count: int,
) -> None:
    """Persist the semantic summary without changing session list ordering."""
    await db.execute(
        """
        UPDATE sessions
        SET context_summary = ?,
            context_summary_message_count = ?,
            context_summary_updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
        """,
        (
            summary.strip(),
            max(0, int(covered_message_count)),
            _now_iso(),
            session_id,
        ),
    )
    await db.commit()


async def maybe_update_title_from_user_message(
    db: aiosqlite.Connection,
    session_id: str,
    message: str,
) -> str | None:
    title = optimize_session_title(message)
    if not title:
        return None
    now = _now_iso()
    cursor = await db.execute(
        """
        UPDATE sessions
        SET title = ?, updated_at = ?
        WHERE id = ?
          AND deleted_at IS NULL
          AND (title IS NULL OR title IN (?, ?, ?, ?))
        """,
        (title, now, session_id, *_AUTO_TITLE_PLACEHOLDERS),
    )
    await db.commit()
    return title if cursor.rowcount > 0 else None


def optimize_session_title(text: str) -> str:
    title = (text or "").strip()
    if not title:
        return ""
    prefixes = (
        "帮我", "请帮我", "麻烦", "查询一下", "查一下", "看一下", "看下",
        "查看一下", "查看", "查询", "帮忙", "我想", "请",
    )
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if title.startswith(prefix):
                title = title[len(prefix):].strip()
                changed = True
    title = title.strip(" ，。！？,.!?")
    if title.lower().startswith("ssh "):
        title = _title_from_ssh_command(title)
    return _compact_title(title)


def _title_from_ssh_command(command: str) -> str:
    parts = command.split(maxsplit=2)
    if len(parts) < 3:
        return command
    alias = parts[1]
    actual = parts[2].strip().strip("'\"")
    first = actual.split("|", 1)[0].strip()
    return f"{alias} · {first}"


def _compact_title(title: str) -> str:
    title = " ".join((title or "").split())
    return title[:32]
