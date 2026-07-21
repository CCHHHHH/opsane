"""Durable state for transfers of chat-session files to SSH targets."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import aiosqlite


ACTIVE_TRANSFER_STATUSES = {"waiting_confirm", "pending", "running"}
TERMINAL_TRANSFER_STATUSES = {"success", "failed", "interrupted", "cancelled"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def create_file_transfer(
    db: aiosqlite.Connection,
    *,
    request_id: str,
    session_id: str,
    file_id: str,
    file_name: str,
    target: str,
    remote_dir: str,
    remote_name: str,
    remote_path: str,
    overwrite: bool,
    size: int,
    sha256: str,
    target_env: str = "",
    target_fingerprint: str = "",
    initial_status: str = "pending",
    source: str = "web",
    turn_id: str = "",
) -> tuple[dict, bool]:
    """Create a transfer once per session/request id.

    Returns ``(record, created)``. A concurrent duplicate returns the existing
    record and never creates a second upload operation.
    """
    transfer_id = f"xfer_{uuid4().hex[:16]}"
    now = _now_iso()
    if initial_status not in {"waiting_confirm", "pending"}:
        raise ValueError(f"非法文件传输初始状态: {initial_status}")
    cursor = await db.execute(
        """
        INSERT OR IGNORE INTO session_file_transfers (
            id, request_id, session_id, file_id, file_name, target, target_env,
            target_fingerprint,
            remote_dir, remote_name, remote_path, overwrite, status,
            size, sha256, source, turn_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transfer_id,
            request_id,
            session_id,
            file_id,
            file_name,
            target,
            target_env,
            target_fingerprint,
            remote_dir,
            remote_name,
            remote_path,
            int(overwrite),
            initial_status,
            max(0, int(size)),
            sha256,
            source or "web",
            turn_id or None,
            now,
            now,
        ),
    )
    await db.commit()
    record = await get_file_transfer_by_request(db, session_id, request_id)
    if not record:  # pragma: no cover - defensive database invariant
        raise RuntimeError("文件传输任务创建失败")
    return record, cursor.rowcount == 1


async def get_file_transfer(
    db: aiosqlite.Connection,
    transfer_id: str,
    *,
    session_id: str | None = None,
) -> dict | None:
    db.row_factory = aiosqlite.Row
    sql = "SELECT * FROM session_file_transfers WHERE id = ?"
    params: list = [transfer_id]
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_file_transfer_by_request(
    db: aiosqlite.Connection,
    session_id: str,
    request_id: str,
) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM session_file_transfers
        WHERE session_id = ? AND request_id = ?
        """,
        (session_id, request_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_file_transfers(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    limit: int = 100,
) -> list[dict]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM session_file_transfers
        WHERE session_id = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT ?
        """,
        (session_id, max(1, min(int(limit), 500))),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def claim_file_transfer(
    db: aiosqlite.Connection,
    transfer_id: str,
    session_id: str,
) -> tuple[bool, dict | None]:
    """Atomically move one pending transfer into running state."""
    now = _now_iso()
    cursor = await db.execute(
        """
        UPDATE session_file_transfers
        SET status = 'running', updated_at = ?
        WHERE id = ? AND session_id = ? AND status = 'pending'
        """,
        (now, transfer_id, session_id),
    )
    await db.commit()
    return cursor.rowcount == 1, await get_file_transfer(
        db, transfer_id, session_id=session_id
    )


async def confirm_file_transfer(
    db: aiosqlite.Connection,
    transfer_id: str,
    session_id: str,
    *,
    confirmed: bool,
) -> tuple[bool, dict | None]:
    """Atomically resolve a durable conversational transfer confirmation.

    A positive decision moves the record to ``pending`` so the normal transfer
    worker can claim it.  A rejection is terminal.  Concurrent or repeated
    decisions return the current record without changing it a second time.
    """
    now = _now_iso()
    if confirmed:
        cursor = await db.execute(
            """
            UPDATE session_file_transfers
            SET status = 'pending', updated_at = ?
            WHERE id = ? AND session_id = ? AND status = 'waiting_confirm'
            """,
            (now, transfer_id, session_id),
        )
    else:
        cursor = await db.execute(
            """
            UPDATE session_file_transfers
            SET status = 'cancelled', error = '', updated_at = ?, completed_at = ?
            WHERE id = ? AND session_id = ? AND status = 'waiting_confirm'
            """,
            (now, now, transfer_id, session_id),
        )
    await db.commit()
    return cursor.rowcount == 1, await get_file_transfer(
        db, transfer_id, session_id=session_id
    )


async def get_waiting_file_transfer(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM session_file_transfers
        WHERE session_id = ? AND status = 'waiting_confirm'
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (session_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def finish_file_transfer(
    db: aiosqlite.Connection,
    transfer_id: str,
    *,
    status: str,
    remote_size: int | None = None,
    remote_sha256: str = "",
    error: str = "",
) -> dict | None:
    if status not in TERMINAL_TRANSFER_STATUSES:
        raise ValueError(f"非法文件传输终态: {status}")
    now = _now_iso()
    await db.execute(
        """
        UPDATE session_file_transfers
        SET status = ?, remote_size = ?, remote_sha256 = ?, error = ?,
            updated_at = ?, completed_at = ?
        WHERE id = ?
        """,
        (
            status,
            remote_size,
            remote_sha256,
            error[:2000],
            now,
            now,
            transfer_id,
        ),
    )
    await db.commit()
    return await get_file_transfer(db, transfer_id)


async def interrupt_running_file_transfers(db: aiosqlite.Connection) -> list[str]:
    """Close transfers left pending/running by a previous process instance."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT id FROM session_file_transfers WHERE status IN ('pending', 'running')"
    )
    ids = [str(row["id"]) for row in await cursor.fetchall()]
    if not ids:
        return []
    now = _now_iso()
    placeholders = ", ".join("?" for _ in ids)
    await db.execute(
        f"""
        UPDATE session_file_transfers
        SET status = 'interrupted', error = ?, updated_at = ?, completed_at = ?
        WHERE id IN ({placeholders}) AND status IN ('pending', 'running')
        """,
        ["Opsane 重启，传输状态已收口", now, now, *ids],
    )
    await db.commit()
    return ids


async def has_active_file_transfer(
    db: aiosqlite.Connection,
    file_id: str,
) -> bool:
    placeholders = ", ".join("?" for _ in ACTIVE_TRANSFER_STATUSES)
    cursor = await db.execute(
        f"""
        SELECT 1 FROM session_file_transfers
        WHERE file_id = ? AND status IN ({placeholders}) LIMIT 1
        """,
        [file_id, *sorted(ACTIVE_TRANSFER_STATUSES)],
    )
    return await cursor.fetchone() is not None


async def has_active_session_file_transfer(
    db: aiosqlite.Connection,
    session_id: str,
) -> bool:
    placeholders = ", ".join("?" for _ in ACTIVE_TRANSFER_STATUSES)
    cursor = await db.execute(
        f"""
        SELECT 1 FROM session_file_transfers
        WHERE session_id = ? AND status IN ({placeholders}) LIMIT 1
        """,
        [session_id, *sorted(ACTIVE_TRANSFER_STATUSES)],
    )
    return await cursor.fetchone() is not None
