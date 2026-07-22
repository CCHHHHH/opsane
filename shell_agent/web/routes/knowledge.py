"""Knowledge conflicts and service-profile candidate review endpoints."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from shell_agent.core.models import AuditRecord
from shell_agent.safety.audit import write_audit
from shell_agent.storage.memories import (
    count_memory_issues,
    list_memory_issues,
    maintain_memories,
    mark_memories_promoted,
)
from shell_agent.storage.profile_candidates import (
    count_profile_candidates,
    expire_stale_profile_candidates,
    get_profile_candidate,
    list_profile_candidates,
    rebase_profile_candidate,
    update_profile_candidate_status,
)
from shell_agent.web.routes.inventory import (
    ServiceRevisionConflict,
    apply_service_profile_changes,
    read_inventory_file,
    write_inventory_file,
)
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import ProfileCandidateDecision


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


def _service_revisions() -> dict[str, int]:
    revisions: dict[str, int] = {}
    for item in read_inventory_file().get("services", []):
        service_id = str(item.get("id") or "")
        if not service_id:
            continue
        try:
            revisions[service_id] = max(1, int(item.get("revision") or 1))
        except (TypeError, ValueError):
            revisions[service_id] = 1
    return revisions


async def _expire_outdated_candidates() -> list[str]:
    rt = get_runtime()
    if not rt.db:
        return []
    return await expire_stale_profile_candidates(rt.db, _service_revisions())


def _expired_candidate_message(candidate: dict) -> str:
    before = candidate.get("before_snapshot") or {}
    expected_revision = int(before.get("revision") or 0)
    current_revision = _service_revisions().get(
        str(candidate.get("service_id") or ""), 0
    )
    return (
        "画像候选已过期，"
        f"基于 revision={expected_revision}，当前 revision={current_revision}；"
        "候选列表已刷新"
    )


def _current_service_snapshot(service_id: str) -> dict:
    return next(
        (
            item
            for item in read_inventory_file().get("services", [])
            if str(item.get("id") or "") == service_id
        ),
        {},
    )


def _rebase_conflicts(candidate: dict, current: dict) -> dict[str, dict]:
    proposed = candidate.get("proposed_changes") or {}
    before = candidate.get("before_snapshot") or {}
    conflicts: dict[str, dict] = {}
    for key, proposed_value in proposed.items():
        before_value = before.get(key)
        current_value = current.get(key)
        if current_value != before_value and current_value != proposed_value:
            conflicts[key] = {
                "before": before_value,
                "current": current_value,
                "proposed": proposed_value,
            }
    current_servers = set(current.get("servers") or [])
    proposed_servers = set(proposed.get("servers") or [])
    if current and proposed_servers and current_servers.isdisjoint(proposed_servers):
        conflicts["servers"] = {
            "before": before.get("servers") or [],
            "current": sorted(current_servers),
            "proposed": sorted(proposed_servers),
            "reason": "候选属于另一台服务器上的独立服务实例",
        }
    return conflicts


@router.get("/api/service-profile-candidates")
async def get_candidates(
    status: str = "pending",
    limit: int = 100,
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"candidates": []}
    try:
        await _expire_outdated_candidates()
        if page is None and page_size is None:
            items = await list_profile_candidates(
                rt.db,
                status=status,
                limit=max(1, min(limit, 500)),
            )
            return {"candidates": items}
        total = await count_profile_candidates(rt.db, status=status)
        effective_size, offset, pagination = _page_window(
            total, page or 1, page_size or 20
        )
        items = await list_profile_candidates(
            rt.db,
            status=status,
            limit=effective_size,
            offset=offset,
        )
    except ValueError as exc:
        return {"candidates": [], "error": str(exc)}
    return {"candidates": items, "pagination": pagination}


@router.get("/api/service-profile-candidates/{candidate_id}")
async def get_candidate(candidate_id: str) -> dict:
    rt = get_runtime()
    await _expire_outdated_candidates()
    item = await get_profile_candidate(rt.db, candidate_id) if rt.db else None
    if not item:
        raise HTTPException(status_code=404, detail="画像候选不存在")
    return {"candidate": item}


@router.post("/api/service-profile-candidates/{candidate_id}/reject")
async def reject_candidate(candidate_id: str) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    item = await get_profile_candidate(rt.db, candidate_id)
    if not item:
        raise HTTPException(status_code=404, detail="画像候选不存在")
    if item.get("status") != "pending":
        return {"ok": False, "error": "画像候选已经审核"}
    updated = await update_profile_candidate_status(rt.db, candidate_id, "rejected")
    return {"ok": True, "candidate": updated}


@router.post("/api/service-profile-candidates/{candidate_id}/rebase")
async def rebase_candidate(candidate_id: str) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    await _expire_outdated_candidates()
    candidate = await get_profile_candidate(rt.db, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="画像候选不存在")
    if candidate.get("status") != "expired":
        return {"ok": False, "error": "只有已过期候选可以重新合并"}

    current = _current_service_snapshot(str(candidate.get("service_id") or ""))
    conflicts = _rebase_conflicts(candidate, current)
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "候选与最新服务画像存在冲突，无法自动重新合并",
                "conflicts": conflicts,
            },
        )
    rebased = await rebase_profile_candidate(rt.db, candidate_id, current)
    return {"ok": True, "candidate": rebased}


@router.post("/api/service-profile-candidates/{candidate_id}/accept")
async def accept_candidate(
    candidate_id: str,
    decision: ProfileCandidateDecision | None = None,
) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    await _expire_outdated_candidates()
    candidate = await get_profile_candidate(rt.db, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="画像候选不存在")
    if candidate.get("status") == "expired":
        raise HTTPException(
            status_code=409,
            detail=_expired_candidate_message(candidate),
        )
    if candidate.get("status") != "pending":
        return {"ok": False, "error": "画像候选已经审核"}

    changes = (
        decision.proposed_changes
        if decision and decision.proposed_changes is not None
        else candidate.get("proposed_changes") or {}
    )
    before = candidate.get("before_snapshot") or {}
    expected_revision = (
        decision.expected_revision
        if decision and decision.expected_revision is not None
        else int(before.get("revision") or 0)
    )
    original_inventory = read_inventory_file()
    try:
        service = await apply_service_profile_changes(
            service_id=str(candidate.get("service_id") or ""),
            service_name=str(candidate.get("service_name") or ""),
            changes=changes,
            expected_revision=expected_revision,
            source_task_id=str(candidate.get("source_task_id") or ""),
        )
    except ServiceRevisionConflict as exc:
        await _expire_outdated_candidates()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        await mark_memories_promoted(rt.db, candidate.get("source_memory_ids") or [])
        updated = await update_profile_candidate_status(
            rt.db,
            candidate_id,
            "accepted",
            proposed_changes=changes,
        )
        await write_audit(
            rt.db,
            AuditRecord(
                session_id="",
                caller="local-user",
                source="service_profile_review",
                target=service.get("id") or candidate.get("service_name") or "",
                target_env=service.get("env") or "",
                executor="internal",
                command=f"accept service profile candidate {candidate_id}",
                executed=True,
                user_confirmed=True,
                exit_code=0,
                stdout=json.dumps(
                    {"before": before, "after": service},
                    ensure_ascii=False,
                ),
            ),
        )
    except Exception:
        write_inventory_file(original_inventory)
        await rt.reload()
        await update_profile_candidate_status(rt.db, candidate_id, "pending", reviewed_by="")
        raise
    await _expire_outdated_candidates()
    return {"ok": True, "candidate": updated, "service": service}


@router.get("/api/knowledge/conflicts")
async def get_knowledge_conflicts(
    limit: int = 100,
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"issues": []}
    if page is None and page_size is None:
        return {"issues": await list_memory_issues(rt.db, max(1, min(limit, 500)))}
    await maintain_memories(rt.db)
    total = await count_memory_issues(rt.db)
    effective_size, offset, pagination = _page_window(
        total, page or 1, page_size or 20
    )
    return {
        "issues": await list_memory_issues(
            rt.db, effective_size, offset=offset
        ),
        "pagination": pagination,
    }
