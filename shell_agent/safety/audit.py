"""审计日志"""
from __future__ import annotations

import aiosqlite

from shell_agent.core.models import AuditRecord


async def write_audit(db: aiosqlite.Connection, record: AuditRecord) -> None:
    """写入审计记录"""
    await db.execute(
        """
        INSERT INTO audit_logs (
            id, session_id, caller, source, target, target_env, executor,
            command, executed, user_confirmed, exit_code, duration_ms,
            stdout, stderr, truncated, timed_out, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.id, record.session_id, record.caller, record.source,
            record.target, record.target_env, record.executor,
            record.command, int(record.executed),
            int(record.user_confirmed) if record.user_confirmed is not None else None,
            record.exit_code, record.duration_ms,
            record.stdout, record.stderr,
            int(record.truncated), int(record.timed_out),
            record.timestamp,
        ),
    )
    await db.commit()


async def query_audit(
    db: aiosqlite.Connection,
    target: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """查询审计记录"""
    sql = "SELECT * FROM audit_logs"
    params: list = []
    if target:
        sql += " WHERE target = ?"
        params.append(target)
    sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([max(1, limit), max(0, offset)])
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def count_audit(
    db: aiosqlite.Connection,
    target: str | None = None,
) -> int:
    """Count audit records using the same target filter as ``query_audit``."""
    sql = "SELECT COUNT(*) FROM audit_logs"
    params: list = []
    if target:
        sql += " WHERE target = ?"
        params.append(target)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0
