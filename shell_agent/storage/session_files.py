"""Persistence helpers for files attached to chat sessions."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

import aiosqlite


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _decode_row(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    raw_metadata = item.get("metadata")
    try:
        item["metadata"] = json.loads(raw_metadata) if raw_metadata else {}
    except (TypeError, json.JSONDecodeError):
        item["metadata"] = {}
    return item


async def create_session_file(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    original_name: str,
    stored_path: str,
    media_type: str,
    extension: str,
    size: int,
    sha256: str,
) -> dict:
    file_id = f"file_{uuid4().hex[:16]}"
    now = _now_iso()
    await db.execute(
        """
        INSERT INTO session_files (
            id, session_id, original_name, stored_path, media_type, extension,
            size, sha256, parse_status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            file_id,
            session_id,
            original_name,
            stored_path,
            media_type,
            extension,
            max(0, int(size)),
            sha256,
            now,
            now,
        ),
    )
    await db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    await db.commit()
    return (await get_session_file(db, file_id)) or {}


async def update_session_file_parse(
    db: aiosqlite.Connection,
    file_id: str,
    *,
    kind: str,
    preview_type: str,
    parse_status: str,
    parse_error: str = "",
    extracted_text: str = "",
    metadata: dict | None = None,
) -> dict | None:
    now = _now_iso()
    cursor = await db.execute(
        """
        UPDATE session_files
        SET kind = ?, preview_type = CASE
                WHEN layout_preview_status = 'ready'
                     AND layout_preview_source_sha256 = sha256 THEN 'pdf'
                ELSE ?
            END,
            parse_status = ?, parse_error = ?,
            extracted_text = ?, metadata = ?, updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
        """,
        (
            kind,
            preview_type,
            parse_status,
            parse_error,
            extracted_text,
            json.dumps(metadata or {}, ensure_ascii=False),
            now,
            file_id,
        ),
    )
    await db.commit()
    if cursor.rowcount != 1:
        return None
    return await get_session_file(db, file_id)


async def claim_session_file_reanalysis(
    db: aiosqlite.Connection,
    file_id: str,
) -> bool:
    """Atomically claim a previously completed attachment for reanalysis."""
    now = _now_iso()
    cursor = await db.execute(
        """
        UPDATE session_files
        SET parse_status = 'pending', parse_error = '', updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
          AND parse_status IN ('metadata_only', 'unsupported', 'error')
          AND COALESCE(layout_preview_status, 'none') != 'pending'
        """,
        (now, file_id),
    )
    await db.commit()
    return cursor.rowcount == 1


async def claim_session_file_layout_preview(
    db: aiosqlite.Connection,
    file_id: str,
) -> str | None:
    """Atomically claim an Office layout render, recovering stale claims."""
    now_value = datetime.now()
    now = now_value.isoformat(timespec="seconds")
    stale_before = (now_value - timedelta(minutes=5)).isoformat(timespec="seconds")
    claim_id = f"preview_{uuid4().hex}"
    cursor = await db.execute(
        """
        UPDATE session_files
        SET layout_preview_status = 'pending', layout_preview_error = '',
            layout_preview_claim_id = ?, layout_preview_updated_at = ?, updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
          AND parse_status != 'pending'
          AND (
            COALESCE(layout_preview_status, 'none') != 'pending'
            OR layout_preview_updated_at IS NULL
            OR layout_preview_updated_at < ?
          )
        """,
        (claim_id, now, now, file_id, stale_before),
    )
    await db.commit()
    return claim_id if cursor.rowcount == 1 else None


async def update_session_file_layout_preview(
    db: aiosqlite.Connection,
    file_id: str,
    *,
    status: str,
    path: str = "",
    error: str = "",
    size: int = 0,
    source_sha256: str = "",
    claim_id: str = "",
) -> dict | None:
    now = _now_iso()
    claim_filter = " AND layout_preview_claim_id = ?" if claim_id else ""
    parameters = [
        status,
        path,
        error,
        max(0, int(size)),
        source_sha256,
        now,
        status,
        status,
        now,
        file_id,
    ]
    if claim_id:
        parameters.append(claim_id)
    cursor = await db.execute(
        """
        UPDATE session_files
        SET layout_preview_status = ?, layout_preview_path = ?,
            layout_preview_error = ?, layout_preview_size = ?,
            layout_preview_source_sha256 = ?, layout_preview_updated_at = ?,
            layout_preview_claim_id = NULL,
            preview_type = CASE
                WHEN ? = 'ready' THEN 'pdf'
                WHEN preview_type = 'pdf' AND ? != 'ready' THEN
                    CASE WHEN COALESCE(extracted_text, '') != '' THEN 'text' ELSE 'none' END
                ELSE preview_type
            END,
            updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
        """ + claim_filter,
        tuple(parameters),
    )
    await db.commit()
    if cursor.rowcount != 1:
        return None
    return await get_session_file(db, file_id)


async def list_session_files(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    include_content: bool = False,
) -> list[dict]:
    db.row_factory = aiosqlite.Row
    columns = "*" if include_content else """
        id, session_id, original_name, media_type, extension, kind, preview_type,
        size, sha256, parse_status, parse_error, metadata,
        layout_preview_status, layout_preview_path, layout_preview_error,
        layout_preview_size, layout_preview_source_sha256, layout_preview_updated_at,
        created_at, updated_at
    """
    cursor = await db.execute(
        f"""
        SELECT {columns}
        FROM session_files
        WHERE session_id = ? AND deleted_at IS NULL
        ORDER BY created_at DESC, rowid DESC
        """,
        (session_id,),
    )
    return [item for row in await cursor.fetchall() if (item := _decode_row(row))]


async def get_session_file(db: aiosqlite.Connection, file_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM session_files WHERE id = ? AND deleted_at IS NULL",
        (file_id,),
    )
    return _decode_row(await cursor.fetchone())


async def get_session_file_for_session(
    db: aiosqlite.Connection,
    session_id: str,
    file_id: str,
) -> dict | None:
    """Return a live file only when it belongs to the requested session."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM session_files
        WHERE id = ? AND session_id = ? AND deleted_at IS NULL
        """,
        (file_id, session_id),
    )
    return _decode_row(await cursor.fetchone())


async def count_session_files(db: aiosqlite.Connection, session_id: str) -> int:
    cursor = await db.execute(
        "SELECT COUNT(*) FROM session_files WHERE session_id = ? AND deleted_at IS NULL",
        (session_id,),
    )
    row = await cursor.fetchone()
    return int(row[0] if row else 0)


async def soft_delete_session_file(db: aiosqlite.Connection, file_id: str) -> dict | None:
    item = await get_session_file(db, file_id)
    if not item:
        return None
    now = _now_iso()
    await db.execute(
        "UPDATE session_files SET deleted_at = ?, updated_at = ? WHERE id = ?",
        (now, now, file_id),
    )
    await db.commit()
    return item


async def soft_delete_all_session_files(
    db: aiosqlite.Connection,
    session_id: str,
) -> list[dict]:
    items = await list_session_files(db, session_id, include_content=True)
    if not items:
        return []
    now = _now_iso()
    await db.execute(
        """
        UPDATE session_files SET deleted_at = ?, updated_at = ?
        WHERE session_id = ? AND deleted_at IS NULL
        """,
        (now, now, session_id),
    )
    await db.commit()
    return items
