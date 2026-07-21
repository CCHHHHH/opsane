"""Audit-query REST endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from shell_agent.safety.audit import query_audit
from shell_agent.web.runtime import get_runtime


router = APIRouter()


@router.get("/api/audit")
async def get_audit(target: str | None = None, limit: int = 50) -> dict:
    rt = get_runtime()
    records = await query_audit(rt.db, target=target, limit=limit)
    return {"records": records}
