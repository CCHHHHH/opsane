"""History-derived Skill candidate discovery and review APIs."""
from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException

from shell_agent.skills.discovery import discover_skill_candidates
from shell_agent.storage.skill_candidates import (
    count_skill_candidates,
    expire_skill_candidates,
    get_skill_candidate,
    list_skill_candidates,
    review_skill_candidate,
)
from shell_agent.web.routes.skills import (
    _parse_skill_yaml,
    _skill_path,
    _write_skill_config_audit,
    preview_skill,
)
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import SkillCandidateScanRequest, SkillPreviewRequest


router = APIRouter()


def _pagination(total: int, page: int, page_size: int) -> tuple[int, int, dict]:
    page_size = max(1, min(page_size, 100))
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    return page_size, (page - 1) * page_size, {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": pages,
    }


@router.post("/api/skill-candidates/scan")
async def scan_skill_candidates(request: SkillCandidateScanRequest) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    await expire_skill_candidates(rt.db)
    result = await discover_skill_candidates(
        rt.db,
        days=request.days,
        min_occurrences=request.min_occurrences,
        secret_values=rt.secret_values() if hasattr(rt, "secret_values") else [],
        semantic=request.semantic,
        llm=getattr(rt, "llm", None),
    )
    return {"ok": True, **result}


@router.get("/api/skill-candidates")
async def get_skill_candidates(
    status: str = "pending",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"candidates": [], "pagination": _pagination(0, 1, page_size)[2]}
    await expire_skill_candidates(rt.db)
    try:
        total = await count_skill_candidates(rt.db, status=status)
        limit, offset, pagination = _pagination(total, page, page_size)
        items = await list_skill_candidates(
            rt.db, status=status, limit=limit, offset=offset
        )
    except ValueError as exc:
        return {"candidates": [], "error": str(exc)}
    return {"candidates": items, "pagination": pagination}


@router.get("/api/skill-candidates/{candidate_id}")
async def get_skill_candidate_detail(candidate_id: str) -> dict:
    rt = get_runtime()
    item = await get_skill_candidate(rt.db, candidate_id) if rt.db else None
    if not item:
        raise HTTPException(status_code=404, detail="Skill 候选不存在")
    return {"candidate": item}


@router.post("/api/skill-candidates/{candidate_id}/preview")
async def preview_skill_candidate(candidate_id: str) -> dict:
    rt = get_runtime()
    candidate = await get_skill_candidate(rt.db, candidate_id) if rt.db else None
    if not candidate:
        raise HTTPException(status_code=404, detail="Skill 候选不存在")
    data = yaml.safe_load(str(candidate.get("draft_yaml") or "")) or {}
    triggers = data.get("triggers") if isinstance(data, dict) else []
    test_input = str(triggers[0]) if isinstance(triggers, list) and triggers else ""
    if not test_input:
        return {"ok": False, "error": "候选没有可用于预览的触发词"}
    result = await preview_skill(
        SkillPreviewRequest(yaml=candidate["draft_yaml"], input=test_input)
    )
    return {**result, "candidate_id": candidate_id, "test_input": test_input}


@router.post("/api/skill-candidates/{candidate_id}/reject")
async def reject_skill_candidate(candidate_id: str) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    item = await get_skill_candidate(rt.db, candidate_id)
    if not item:
        raise HTTPException(status_code=404, detail="Skill 候选不存在")
    updated = await review_skill_candidate(rt.db, candidate_id, "rejected")
    if not updated:
        return {"ok": False, "error": "候选已审核或过期"}
    return {"ok": True, "candidate": updated}


@router.post("/api/skill-candidates/{candidate_id}/accept")
async def accept_skill_candidate(candidate_id: str) -> dict:
    """Publish an accepted draft as a disabled Skill after strict validation."""
    rt = get_runtime()
    if not rt.db:
        return {"ok": False, "error": "数据库未初始化"}
    candidate = await get_skill_candidate(rt.db, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Skill 候选不存在")
    if candidate.get("status") != "pending":
        return {"ok": False, "error": "候选已审核或过期"}
    try:
        data = yaml.safe_load(str(candidate.get("draft_yaml") or "")) or {}
        if not isinstance(data, dict):
            raise ValueError("Skill 草稿格式无效")
        data["enabled"] = False
        raw_yaml = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        skill = _parse_skill_yaml(raw_yaml)
        path = _skill_path(skill.name)
        if path.exists():
            return {"ok": False, "error": f"Skill {skill.name} 已存在，请先处理名称冲突"}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(raw_yaml)
        await _write_skill_config_audit(rt, "accept candidate and create disabled", skill)
        updated = await review_skill_candidate(
            rt.db,
            candidate_id,
            "accepted",
            published_skill_name=skill.name,
        )
        if not updated:
            path.unlink(missing_ok=True)
            return {"ok": False, "error": "候选状态已变化，请刷新后重试"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "candidate": updated,
        "skill": {"name": skill.name, "enabled": False, "source": str(path)},
    }
