"""Global-memory persistence, lifecycle, and conflict helpers."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from uuid import uuid4

import aiosqlite


MEMORY_TYPES = {"fact", "procedure", "preference", "observation"}
MEMORY_STATUSES = {"inferred", "confirmed", "promoted", "stale", "conflicted"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id() -> str:
    return f"mem_{uuid4().hex[:12]}"


def normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def memory_fingerprint(
    memory_type: str,
    subject: str,
    predicate: str,
    target: str = "",
) -> str:
    raw = "|".join(
        [
            normalize_key(memory_type) or "fact",
            normalize_key(subject),
            normalize_key(predicate) or "note",
            normalize_key(target),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _normalize_type(value: str) -> str:
    normalized = normalize_key(value) or "fact"
    if normalized not in MEMORY_TYPES:
        raise ValueError(f"不支持的记忆类型: {value}")
    return normalized


def _normalize_status(value: str) -> str:
    normalized = normalize_key(value) or "confirmed"
    if normalized not in MEMORY_STATUSES:
        raise ValueError(f"不支持的记忆状态: {value}")
    return normalized


async def upsert_memory(
    db: aiosqlite.Connection,
    *,
    subject: str,
    predicate: str,
    value: str,
    target: str = "",
    memory_type: str = "fact",
    status: str = "confirmed",
    confidence: float = 1.0,
    source_session_id: str = "",
    source_task_id: str = "",
    source_event_id: str = "",
    source: str = "manual",
    observed_at: str = "",
    expires_at: str = "",
    evidence_summary: str = "",
) -> dict:
    subject = normalize_key(subject)
    predicate = normalize_key(predicate) or "note"
    value = (value or "").strip()
    target = (target or "").strip()
    memory_type = _normalize_type(memory_type)
    status = _normalize_status(status)
    confidence = max(0.0, min(float(confidence), 1.0))
    if not subject:
        raise ValueError("subject 不能为空")
    if not value:
        raise ValueError("value 不能为空")

    fingerprint = memory_fingerprint(memory_type, subject, predicate, target)
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM global_memories
        WHERE fingerprint = ? AND value = ? AND deleted_at IS NULL
        LIMIT 1
        """,
        (fingerprint, value),
    )
    row = await cursor.fetchone()
    if not row:
        # Compatibility for rows created before fingerprints were introduced.
        cursor = await db.execute(
            """
            SELECT * FROM global_memories
            WHERE subject = ? AND predicate = ? AND value = ?
              AND COALESCE(target, '') = ? AND deleted_at IS NULL
            LIMIT 1
            """,
            (subject, predicate, value, target),
        )
        row = await cursor.fetchone()

    now = _now_iso()
    observed_at = observed_at or now
    if row:
        current_status = str(row["status"] or "confirmed")
        if current_status in {"promoted", "confirmed", "conflicted"} and status == "inferred":
            status = current_status
        confidence = max(confidence, float(row["confidence"] or 0.0))
        await db.execute(
            """
            UPDATE global_memories
            SET target = ?, type = ?, status = ?, confidence = ?,
                source_session_id = ?, source_task_id = ?, source_event_id = ?,
                source = ?, observed_at = ?, expires_at = ?,
                evidence_summary = ?, fingerprint = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                target,
                memory_type,
                status,
                confidence,
                source_session_id,
                source_task_id,
                source_event_id,
                source,
                observed_at,
                expires_at,
                evidence_summary,
                fingerprint,
                now,
                row["id"],
            ),
        )
        await db.commit()
        return await get_memory(db, row["id"]) or dict(row)

    memory_id = _gen_id()
    await db.execute(
        """
        INSERT INTO global_memories (
            id, subject, predicate, value, target, type, status, confidence,
            source_session_id, source_task_id, source_event_id, source,
            observed_at, expires_at, evidence_summary, fingerprint,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            subject,
            predicate,
            value,
            target,
            memory_type,
            status,
            confidence,
            source_session_id,
            source_task_id,
            source_event_id,
            source,
            observed_at,
            expires_at,
            evidence_summary,
            fingerprint,
            now,
            now,
        ),
    )
    await db.commit()
    await _mark_fingerprint_conflicts(db, fingerprint)
    return await get_memory(db, memory_id) or {}


async def _mark_fingerprint_conflicts(db: aiosqlite.Connection, fingerprint: str) -> None:
    cursor = await db.execute(
        """
        SELECT COUNT(DISTINCT value)
        FROM global_memories
        WHERE fingerprint = ? AND deleted_at IS NULL
          AND status NOT IN ('stale', 'promoted')
        """,
        (fingerprint,),
    )
    row = await cursor.fetchone()
    if row and int(row[0] or 0) > 1:
        await db.execute(
            """
            UPDATE global_memories
            SET status = 'conflicted', updated_at = ?
            WHERE fingerprint = ? AND deleted_at IS NULL
              AND status NOT IN ('stale', 'promoted')
            """,
            (_now_iso(), fingerprint),
        )
        await db.commit()


async def get_memory(db: aiosqlite.Connection, memory_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM global_memories WHERE id = ? AND deleted_at IS NULL",
        (memory_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_memory(
    db: aiosqlite.Connection,
    memory_id: str,
    changes: dict,
) -> dict | None:
    current = await get_memory(db, memory_id)
    if not current:
        return None
    allowed = {
        "subject",
        "predicate",
        "value",
        "target",
        "type",
        "status",
        "confidence",
        "expires_at",
        "evidence_summary",
    }
    payload = {key: value for key, value in changes.items() if key in allowed and value is not None}
    if not payload:
        return current
    merged = {**current, **payload}
    memory_type = _normalize_type(str(merged.get("type") or "fact"))
    status = _normalize_status(str(merged.get("status") or "confirmed"))
    subject = normalize_key(str(merged.get("subject") or ""))
    predicate = normalize_key(str(merged.get("predicate") or "note")) or "note"
    value = str(merged.get("value") or "").strip()
    target = str(merged.get("target") or "").strip()
    if not subject or not value:
        raise ValueError("subject 和 value 不能为空")
    confidence = max(0.0, min(float(merged.get("confidence") or 0.0), 1.0))
    fingerprint = memory_fingerprint(memory_type, subject, predicate, target)
    await db.execute(
        """
        UPDATE global_memories
        SET subject = ?, predicate = ?, value = ?, target = ?, type = ?,
            status = ?, confidence = ?, expires_at = ?, evidence_summary = ?,
            fingerprint = ?, updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
        """,
        (
            subject,
            predicate,
            value,
            target,
            memory_type,
            status,
            confidence,
            str(merged.get("expires_at") or ""),
            str(merged.get("evidence_summary") or ""),
            fingerprint,
            _now_iso(),
            memory_id,
        ),
    )
    await db.commit()
    await _mark_fingerprint_conflicts(db, fingerprint)
    return await get_memory(db, memory_id)


async def mark_memories_promoted(
    db: aiosqlite.Connection,
    memory_ids: list[str],
) -> None:
    ids = [item for item in memory_ids if item]
    if not ids:
        return
    placeholders = ", ".join("?" for _ in ids)
    await db.execute(
        f"UPDATE global_memories SET status = 'promoted', updated_at = ? WHERE id IN ({placeholders})",
        [_now_iso(), *ids],
    )
    await db.commit()


async def mark_expired_memories(db: aiosqlite.Connection) -> int:
    cursor = await db.execute(
        """
        UPDATE global_memories
        SET status = 'stale', updated_at = ?
        WHERE deleted_at IS NULL
          AND COALESCE(expires_at, '') != ''
          AND expires_at <= ?
          AND status NOT IN ('stale', 'promoted')
        """,
        (_now_iso(), _now_iso()),
    )
    await db.commit()
    return cursor.rowcount


async def search_memories(
    db: aiosqlite.Connection,
    query: str = "",
    *,
    target: str = "",
    memory_type: str = "",
    status: str = "",
    usable_only: bool = False,
    limit: int = 8,
) -> list[dict]:
    await mark_expired_memories(db)
    terms = extract_memory_terms(query)
    sql = "SELECT * FROM global_memories WHERE deleted_at IS NULL"
    params: list = []
    if target:
        sql += " AND (target = ? OR COALESCE(target, '') = '')"
        params.append(target)
    if memory_type:
        sql += " AND type = ?"
        params.append(_normalize_type(memory_type))
    if status:
        sql += " AND status = ?"
        params.append(_normalize_status(status))
    elif usable_only:
        sql += " AND status IN ('inferred', 'confirmed', 'promoted')"
    if terms:
        clauses = []
        for term in terms[:6]:
            like = f"%{term}%"
            clauses.append("(subject LIKE ? OR predicate LIKE ? OR value LIKE ? OR COALESCE(evidence_summary, '') LIKE ?)")
            params.extend([like, like, like, like])
        sql += " AND (" + " OR ".join(clauses) + ")"
    elif query.strip():
        like = f"%{query.strip().lower()}%"
        sql += " AND (subject LIKE ? OR predicate LIKE ? OR value LIKE ?)"
        params.extend([like, like, like])
    sql += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
    params.append(max(1, limit))
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    return [dict(row) for row in await cursor.fetchall()]


async def list_memories(
    db: aiosqlite.Connection,
    query: str = "",
    limit: int = 100,
    *,
    memory_type: str = "",
    status: str = "",
    target: str = "",
) -> list[dict]:
    return await search_memories(
        db,
        query,
        limit=limit,
        memory_type=memory_type,
        status=status,
        target=target,
    )


async def list_memory_issues(db: aiosqlite.Connection, limit: int = 100) -> list[dict]:
    await mark_expired_memories(db)
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM global_memories
        WHERE deleted_at IS NULL AND status IN ('stale', 'conflicted')
        ORDER BY updated_at DESC LIMIT ?
        """,
        (max(1, limit),),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def delete_memory(db: aiosqlite.Connection, memory_id: str) -> bool:
    now = _now_iso()
    cursor = await db.execute(
        """
        UPDATE global_memories
        SET deleted_at = ?, updated_at = ?
        WHERE id = ? AND deleted_at IS NULL
        """,
        (now, now, memory_id),
    )
    await db.commit()
    return cursor.rowcount > 0


def extract_memory_terms(text: str) -> list[str]:
    text = normalize_key(text)
    if not text:
        return []
    tokens = re.findall(r"[a-z0-9_.@+-]+|[\u4e00-\u9fff]{2,}", text)
    stop = {
        "帮我",
        "一下",
        "这个",
        "那个",
        "重启",
        "查看",
        "查询",
        "部署",
        "服务",
        "机器",
        "服务器",
        "看看",
        "哪些",
        "怎么",
        "为什么",
    }
    output: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in stop or len(token) < 2:
            continue
        if token not in seen:
            seen.add(token)
            output.append(token)
    return output
