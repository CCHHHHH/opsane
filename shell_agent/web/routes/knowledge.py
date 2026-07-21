"""Knowledge conflicts and service-profile candidate review endpoints."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from shell_agent.core.models import AuditRecord
from shell_agent.safety.audit import write_audit
from shell_agent.storage.memories import list_memory_issues, mark_memories_promoted
from shell_agent.storage.profile_candidates import (
    get_profile_candidate,
    list_profile_candidates,
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


@router.get("/api/service-profile-candidates")
async def get_candidates(status: str = "pending", limit: int = 100) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"candidates": []}
    try:
        items = await list_profile_candidates(
            rt.db,
            status=status,
            limit=max(1, min(limit, 500)),
        )
    except ValueError as exc:
        return {"candidates": [], "error": str(exc)}
    return {"candidates": items}


@router.get("/api/service-profile-candidates/{candidate_id}")
async def get_candidate(candidate_id: str) -> dict:
    rt = get_runtime()
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


@router.post("/api/service-profile-candidates/{candidate_id}/accept")
async def accept_candidate(
    candidate_id: str,
    decision: ProfileCandidateDecision | None = None,
) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    candidate = await get_profile_candidate(rt.db, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="画像候选不存在")
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
    return {"ok": True, "candidate": updated, "service": service}


@router.get("/api/knowledge/conflicts")
async def get_knowledge_conflicts(limit: int = 100) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"issues": []}
    return {"issues": await list_memory_issues(rt.db, max(1, min(limit, 500)))}
