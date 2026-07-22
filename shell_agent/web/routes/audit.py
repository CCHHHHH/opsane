"""Audit-query REST endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from shell_agent.safety.audit import count_audit, query_audit
from shell_agent.web.runtime import get_runtime


router = APIRouter()


def _page_window(total: int, page: int, page_size: int) -> tuple[int, int, dict]:
    page_size = max(1, min(page_size, 100))
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    return page_size, (page - 1) * page_size, {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


@router.get("/api/audit")
async def get_audit(
    target: str | None = None,
    limit: int = 50,
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    rt = get_runtime()
    if page is None and page_size is None:
        records = await query_audit(
            rt.db,
            target=target,
            limit=max(1, min(limit, 500)),
        )
        return {"records": records}
    total = await count_audit(rt.db, target=target)
    effective_size, offset, pagination = _page_window(
        total,
        page or 1,
        page_size or 20,
    )
    records = await query_audit(
        rt.db,
        target=target,
        limit=effective_size,
        offset=offset,
    )
    return {"records": records, "pagination": pagination}
