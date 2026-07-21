"""Persistence helpers for service-profile change candidates."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import uuid4

import aiosqlite


CANDIDATE_STATUSES = {"pending", "accepted", "rejected", "expired"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id() -> str:
    return f"profile_candidate_{uuid4().hex[:12]}"


def candidate_fingerprint(service_id: str, service_name: str, changes: dict) -> str:
    normalized = json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = f"{service_id.strip().lower()}|{service_name.strip().lower()}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _decode(row: aiosqlite.Row | dict | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    for key, fallback in (
        ("proposed_changes", {}),
        ("before_snapshot", {}),
        ("evidence", {}),
        ("source_memory_ids", []),
    ):
        raw = item.get(key)
        try:
            item[key] = json.loads(raw) if raw else fallback
        except (TypeError, json.JSONDecodeError):
            item[key] = fallback
    return item


async def create_profile_candidate(
    db: aiosqlite.Connection,
    *,
    service_id: str,
    service_name: str,
    proposed_changes: dict,
    before_snapshot: dict | None = None,
    evidence: dict | None = None,
    confidence: float = 1.0,
    source_memory_ids: list[str] | None = None,
    source_task_id: str = "",
) -> tuple[dict, bool]:
    service_name = service_name.strip()
    if not service_name:
        raise ValueError("service_name 不能为空")
    if not proposed_changes:
        raise ValueError("proposed_changes 不能为空")
    fingerprint = candidate_fingerprint(service_id, service_name, proposed_changes)
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM service_profile_candidates
        WHERE fingerprint = ? AND status IN ('pending', 'accepted', 'rejected')
        ORDER BY created_at DESC LIMIT 1
        """,
        (fingerprint,),
    )
    existing = await cursor.fetchone()
    if existing:
        return _decode(existing) or {}, False

    candidate_id = _gen_id()
    now = _now_iso()
    await db.execute(
        """
        INSERT INTO service_profile_candidates (
            id, service_id, service_name, proposed_changes, before_snapshot,
            evidence, confidence, fingerprint, status, source_memory_ids,
            source_task_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            candidate_id,
            service_id.strip(),
            service_name,
            json.dumps(proposed_changes, ensure_ascii=False),
            json.dumps(before_snapshot or {}, ensure_ascii=False),
            json.dumps(evidence or {}, ensure_ascii=False),
            max(0.0, min(float(confidence), 1.0)),
            fingerprint,
            json.dumps(source_memory_ids or [], ensure_ascii=False),
            source_task_id,
            now,
        ),
    )
    await db.commit()
    return await get_profile_candidate(db, candidate_id) or {}, True


async def get_profile_candidate(db: aiosqlite.Connection, candidate_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM service_profile_candidates WHERE id = ?",
        (candidate_id,),
    )
    return _decode(await cursor.fetchone())


async def list_profile_candidates(
    db: aiosqlite.Connection,
    *,
    status: str = "pending",
    limit: int = 100,
) -> list[dict]:
    sql = "SELECT * FROM service_profile_candidates WHERE 1 = 1"
    params: list = []
    if status:
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"不支持的候选状态: {status}")
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, limit))
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    return [_decode(row) or {} for row in await cursor.fetchall()]


async def update_profile_candidate_status(
    db: aiosqlite.Connection,
    candidate_id: str,
    status: str,
    *,
    reviewed_by: str = "local-user",
    proposed_changes: dict | None = None,
) -> dict | None:
    if status not in CANDIDATE_STATUSES:
        raise ValueError(f"不支持的候选状态: {status}")
    assignments = ["status = ?", "reviewed_at = ?", "reviewed_by = ?"]
    params: list = [status, _now_iso(), reviewed_by]
    if proposed_changes is not None:
        assignments.append("proposed_changes = ?")
        params.append(json.dumps(proposed_changes, ensure_ascii=False))
    params.append(candidate_id)
    cursor = await db.execute(
        f"UPDATE service_profile_candidates SET {', '.join(assignments)} WHERE id = ?",
        params,
    )
    await db.commit()
    if cursor.rowcount < 1:
        return None
    return await get_profile_candidate(db, candidate_id)
