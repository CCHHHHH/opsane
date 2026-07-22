"""Persistence for history-derived Skill candidates."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

import aiosqlite


STATUSES = {"pending", "accepted", "rejected", "expired"}


def _now() -> datetime:
    return datetime.now()


def _decode(row) -> dict | None:
    if not row:
        return None
    item = dict(row)
    for key, fallback in (("evidence", {}), ("source_task_ids", [])):
        raw = item.get(key)
        try:
            item[key] = json.loads(raw) if raw else fallback
        except (TypeError, json.JSONDecodeError):
            item[key] = fallback
    return item


async def create_skill_candidate(
    db: aiosqlite.Connection,
    *,
    name: str,
    description: str,
    fingerprint: str,
    draft_yaml: str,
    evidence: dict,
    confidence: float,
    risk_level: str,
    source_task_ids: list[str],
    retention_days: int = 30,
) -> tuple[dict, bool]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM skill_candidates
        WHERE fingerprint = ? AND status IN ('pending', 'accepted', 'rejected')
        ORDER BY created_at DESC LIMIT 1
        """,
        (fingerprint,),
    )
    existing = _decode(await cursor.fetchone())
    if existing:
        return existing, False
    now = _now()
    candidate_id = f"skill_candidate_{uuid4().hex[:12]}"
    await db.execute(
        """
        INSERT INTO skill_candidates (
            id, name, description, fingerprint, status, draft_yaml, evidence,
            confidence, risk_level, occurrence_count, source_task_ids,
            created_at, expires_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            name,
            description,
            fingerprint,
            draft_yaml,
            json.dumps(evidence, ensure_ascii=False),
            max(0.0, min(float(confidence), 1.0)),
            risk_level,
            len(source_task_ids),
            json.dumps(source_task_ids, ensure_ascii=False),
            now.isoformat(timespec="seconds"),
            (now + timedelta(days=max(1, retention_days))).isoformat(timespec="seconds"),
        ),
    )
    await db.commit()
    return await get_skill_candidate(db, candidate_id) or {}, True


async def expire_skill_candidates(db: aiosqlite.Connection) -> int:
    cursor = await db.execute(
        """
        UPDATE skill_candidates
        SET status = 'expired', reviewed_at = ?, reviewed_by = 'system:retention'
        WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?
        """,
        (_now().isoformat(timespec="seconds"), _now().isoformat(timespec="seconds")),
    )
    await db.commit()
    return max(0, cursor.rowcount)


async def get_skill_candidate(db: aiosqlite.Connection, candidate_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM skill_candidates WHERE id = ?", (candidate_id,))
    return _decode(await cursor.fetchone())


async def list_skill_candidates(
    db: aiosqlite.Connection,
    *,
    status: str = "pending",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    if status and status not in STATUSES:
        raise ValueError(f"不支持的 Skill 候选状态: {status}")
    sql = "SELECT * FROM skill_candidates WHERE 1 = 1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(limit, 100)), max(0, offset)])
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    return [_decode(row) or {} for row in await cursor.fetchall()]


async def count_skill_candidates(db: aiosqlite.Connection, *, status: str = "pending") -> int:
    if status and status not in STATUSES:
        raise ValueError(f"不支持的 Skill 候选状态: {status}")
    sql = "SELECT COUNT(*) FROM skill_candidates WHERE 1 = 1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0


async def review_skill_candidate(
    db: aiosqlite.Connection,
    candidate_id: str,
    status: str,
    *,
    reviewed_by: str = "local-user",
    published_skill_name: str = "",
) -> dict | None:
    if status not in {"accepted", "rejected"}:
        raise ValueError("Skill 候选只能接受或拒绝")
    cursor = await db.execute(
        """
        UPDATE skill_candidates
        SET status = ?, reviewed_at = ?, reviewed_by = ?, published_skill_name = ?
        WHERE id = ? AND status = 'pending'
        """,
        (
            status,
            _now().isoformat(timespec="seconds"),
            reviewed_by,
            published_skill_name,
            candidate_id,
        ),
    )
    await db.commit()
    if cursor.rowcount < 1:
        return None
    return await get_skill_candidate(db, candidate_id)
