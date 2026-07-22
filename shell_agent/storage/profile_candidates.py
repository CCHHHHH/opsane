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


def _merge_unique(existing: list, incoming: list) -> list:
    merged: list = []
    for item in [*existing, *incoming]:
        if item not in merged:
            merged.append(item)
    return merged


def _merge_changes(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, list):
            merged[key] = _merge_unique(current, value)
        else:
            merged[key] = value
    return merged


def _merge_evidence(existing: dict, incoming: dict) -> dict:
    merged = {**existing, **incoming}
    summaries = _merge_unique(
        [text for text in [existing.get("summary")] if text],
        [text for text in [incoming.get("summary")] if text],
    )
    if summaries:
        merged["summary"] = "；".join(str(item) for item in summaries)
    return merged


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
    service_id = service_id.strip()
    source_task_id = source_task_id.strip()
    db.row_factory = aiosqlite.Row
    if source_task_id and service_id:
        cursor = await db.execute(
            """
            SELECT * FROM service_profile_candidates
            WHERE source_task_id = ? AND service_id = ? AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
            """,
            (source_task_id, service_id),
        )
        pending = _decode(await cursor.fetchone())
        if pending:
            merged_changes = _merge_changes(
                pending.get("proposed_changes") or {}, proposed_changes
            )
            merged_evidence = _merge_evidence(
                pending.get("evidence") or {}, evidence or {}
            )
            merged_memory_ids = _merge_unique(
                pending.get("source_memory_ids") or [], source_memory_ids or []
            )
            fingerprint = candidate_fingerprint(
                service_id, service_name, merged_changes
            )
            await db.execute(
                """
                UPDATE service_profile_candidates
                SET proposed_changes = ?, evidence = ?, confidence = ?,
                    fingerprint = ?, source_memory_ids = ?
                WHERE id = ?
                """,
                (
                    json.dumps(merged_changes, ensure_ascii=False),
                    json.dumps(merged_evidence, ensure_ascii=False),
                    min(
                        float(pending.get("confidence") or 1.0),
                        max(0.0, min(float(confidence), 1.0)),
                    ),
                    fingerprint,
                    json.dumps(merged_memory_ids, ensure_ascii=False),
                    pending["id"],
                ),
            )
            await db.commit()
            return await get_profile_candidate(db, pending["id"]) or {}, False

    fingerprint = candidate_fingerprint(service_id, service_name, proposed_changes)
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
            service_id,
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


async def expire_stale_profile_candidates(
    db: aiosqlite.Connection,
    current_revisions: dict[str, int],
) -> list[str]:
    """Expire pending candidates whose source snapshot is no longer current."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM service_profile_candidates WHERE status = 'pending'"
    )
    expired_ids: list[str] = []
    for row in await cursor.fetchall():
        candidate = _decode(row) or {}
        service_id = str(candidate.get("service_id") or "")
        before = candidate.get("before_snapshot") or {}
        try:
            expected_revision = int(before.get("revision") or 0)
        except (TypeError, ValueError):
            expected_revision = 0
        current_revision = int(current_revisions.get(service_id, 0))
        if expected_revision != current_revision:
            candidate_id = str(candidate.get("id") or "")
            if candidate_id:
                expired_ids.append(candidate_id)

    if not expired_ids:
        return []
    placeholders = ", ".join("?" for _ in expired_ids)
    await db.execute(
        f"""
        UPDATE service_profile_candidates
        SET status = 'expired', reviewed_at = ?, reviewed_by = ?
        WHERE id IN ({placeholders}) AND status = 'pending'
        """,
        [_now_iso(), "system:revision-conflict", *expired_ids],
    )
    await db.commit()
    return expired_ids


async def rebase_profile_candidate(
    db: aiosqlite.Connection,
    candidate_id: str,
    before_snapshot: dict,
) -> dict | None:
    cursor = await db.execute(
        """
        UPDATE service_profile_candidates
        SET before_snapshot = ?, status = 'pending', reviewed_at = NULL,
            reviewed_by = NULL
        WHERE id = ? AND status = 'expired'
        """,
        (json.dumps(before_snapshot, ensure_ascii=False), candidate_id),
    )
    await db.commit()
    if cursor.rowcount < 1:
        return None
    return await get_profile_candidate(db, candidate_id)


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
    offset: int = 0,
) -> list[dict]:
    sql = "SELECT * FROM service_profile_candidates WHERE 1 = 1"
    params: list = []
    if status:
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"不支持的候选状态: {status}")
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([max(1, limit), max(0, offset)])
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    return [_decode(row) or {} for row in await cursor.fetchall()]


async def count_profile_candidates(
    db: aiosqlite.Connection,
    *,
    status: str = "pending",
) -> int:
    sql = "SELECT COUNT(*) FROM service_profile_candidates WHERE 1 = 1"
    params: list = []
    if status:
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"不支持的候选状态: {status}")
        sql += " AND status = ?"
        params.append(status)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0


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
