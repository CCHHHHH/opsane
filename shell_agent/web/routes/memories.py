"""Global-memory REST endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from shell_agent.storage.memories import (
    count_memories,
    delete_memory,
    list_memories,
    maintain_memories,
    update_memory,
    upsert_memory,
)
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import MemoryCreate, MemoryUpdate


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


@router.get("/api/memories")
async def get_memories(
    q: str = "",
    limit: int = 100,
    type: str = "",
    status: str = "",
    target: str = "",
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"memories": []}
    try:
        if page is None and page_size is None:
            memories = await list_memories(
                rt.db,
                q,
                limit=max(1, min(limit, 500)),
                memory_type=type,
                status=status,
                target=target,
            )
            return {"memories": memories}
        await maintain_memories(rt.db)
        total = await count_memories(
            rt.db,
            q,
            memory_type=type,
            status=status,
            target=target,
        )
        effective_size, offset, pagination = _page_window(
            total, page or 1, page_size or 20
        )
        memories = await list_memories(
            rt.db,
            q,
            limit=effective_size,
            memory_type=type,
            status=status,
            target=target,
            offset=offset,
        )
    except ValueError as exc:
        return {"memories": [], "error": str(exc)}
    return {"memories": memories, "pagination": pagination}


@router.post("/api/memories")
async def create_memory(memory: MemoryCreate) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    try:
        item = await upsert_memory(
            rt.db,
            subject=memory.subject,
            predicate=memory.predicate,
            value=memory.value,
            target=memory.target,
            memory_type=memory.type,
            status=memory.status,
            confidence=memory.confidence,
            expires_at=memory.expires_at,
            evidence_summary=memory.evidence_summary,
            source="manual_api",
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "memory": item}


@router.put("/api/memories/{memory_id}")
async def edit_memory(memory_id: str, memory: MemoryUpdate) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    try:
        item = await update_memory(rt.db, memory_id, memory.model_dump(exclude_none=True))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if not item:
        return {"ok": False, "error": "记忆不存在或已删除"}
    return {"ok": True, "memory": item}


@router.delete("/api/memories/{memory_id}")
async def remove_memory(memory_id: str) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    ok = await delete_memory(rt.db, memory_id)
    return {"ok": ok, "error": "" if ok else "记忆不存在或已删除"}
