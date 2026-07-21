"""HTTP contract for durable registered deployment runbooks.

The route layer resolves only confirmed configuration and session-scoped files.
It never asks the LLM to infer a deploy path, and it never serializes the local
attachment cache path back to the browser.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, status
from loguru import logger

from shell_agent.runbooks import (
    ArtifactSnapshot,
    DeploymentValidationError,
    RunConflictError,
    RunStatus,
    ServiceProfileSnapshot,
)
from shell_agent.storage.session_files import get_session_file_for_session
from shell_agent.storage.sessions import get_session
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import (
    DeploymentPlanConfirm,
    DeploymentRollbackConfirm,
    DeploymentRunCreate,
)


router = APIRouter(prefix="/api/deployment-runs", tags=["deployment-runs"])

MAX_ARTIFACT_SIZE = 512 * 1024 * 1024
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RUN_ID_PATTERN = re.compile(r"^deprun_[A-Za-z0-9_-]{1,80}$")


def _attachment_root() -> Path:
    return Path("data/session_files").resolve()


def _checked_artifact_path(item: dict[str, Any]) -> Path:
    path = Path(str(item.get("stored_path") or "")).resolve()
    root = _attachment_root()
    if (path != root and root not in path.parents) or not path.is_file():
        # Missing and escaped/cross-scope paths deliberately share one result.
        raise HTTPException(status_code=404, detail="会话文件不存在")
    return path


def _file_digest(path: Path) -> tuple[int, str]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ARTIFACT_SIZE:
                raise ValueError("部署制品超过 512 MB 上限")
            digest.update(chunk)
    return size, digest.hexdigest()


async def _deployment_runtime(rt):
    """Return the process-wide runtime initialized by ``Runtime.start``.

    The delayed fallback keeps route imports independent from the production
    SSH adapter and also makes isolated API tests easy to inject.
    """
    current = getattr(rt, "deployment_runtime", None)
    if current is not None:
        return current
    initializer = getattr(rt, "initialize_deployment_runtime", None)
    if initializer is None:
        raise HTTPException(status_code=503, detail="部署服务未初始化")
    current = await initializer()
    if current is None:  # pragma: no cover - defensive runtime invariant
        raise HTTPException(status_code=503, detail="部署服务未初始化")
    return current


def _service_snapshot(rt, service_id: str) -> ServiceProfileSnapshot:
    profile = rt.services.get(service_id)
    if not profile:
        raise HTTPException(status_code=404, detail="服务画像不存在")
    servers = [str(item).strip() for item in profile.servers if str(item).strip()]
    if len(servers) != 1:
        raise HTTPException(status_code=400, detail="首版部署仅支持绑定一台服务器的服务")
    target = rt.executor.resolve_server_alias(servers[0]) if rt.executor else None
    if target is None:
        raise HTTPException(status_code=400, detail="服务画像绑定了未知服务器")
    server = rt.executor.resolve_server(target)
    if str(profile.env).strip().lower() != str(server.env).strip().lower():
        raise HTTPException(status_code=409, detail="服务环境与目标服务器环境不一致")
    return ServiceProfileSnapshot(
        service_id=profile.id,
        service_name=profile.name,
        revision=int(profile.revision),
        verification_status=profile.verification_status,
        environment=str(profile.env).strip().lower(),
        target=target,
        deploy_dir=profile.deploy_dir,
        artifact_path=str(getattr(profile, "artifact_path", "")),
        backup_dir=str(getattr(profile, "backup_dir", "")),
        start_cmd=profile.start_cmd,
        stop_cmd=profile.stop_cmd,
        status_cmd=profile.status_cmd,
        artifact_type=str(getattr(profile, "artifact_type", "jar") or "jar")
        .strip()
        .lower(),
        runtime=str(getattr(profile, "runtime", "") or "").strip().lower(),
        health_url=profile.health_url,
        ports=tuple(int(port) for port in profile.ports),
        startup_timeout_seconds=int(
            getattr(profile, "startup_timeout_seconds", 60) or 60
        ),
    )


async def _artifact_snapshot(
    rt, *, session_id: str, file_id: str
) -> ArtifactSnapshot:
    item = await get_session_file_for_session(rt.db, session_id, file_id)
    if not item:
        # Do not reveal that a file exists in another session.
        raise HTTPException(status_code=404, detail="会话文件不存在")
    local_path = _checked_artifact_path(item)
    try:
        size, actual_sha256 = await asyncio.to_thread(_file_digest, local_path)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    if size <= 0:
        raise HTTPException(status_code=400, detail="部署制品为空")
    if size != int(item.get("size") or 0) or actual_sha256 != str(
        item.get("sha256") or ""
    ).lower():
        raise HTTPException(status_code=409, detail="会话文件内容与上传记录不一致")
    return ArtifactSnapshot(
        file_id=file_id,
        session_id=session_id,
        name=str(item.get("original_name") or ""),
        local_path=str(local_path),
        size=size,
        sha256=actual_sha256,
    )


def _scrub_local_path(value: Any, local_path: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub_local_path(item, local_path)
            for key, item in value.items()
            if key not in {"local_path", "stored_path", "local_cache_path", "service_key"}
        }
    if isinstance(value, list):
        return [_scrub_local_path(item, local_path) for item in value]
    if isinstance(value, tuple):
        return [_scrub_local_path(item, local_path) for item in value]
    if isinstance(value, str) and local_path:
        return value.replace(local_path, "会话制品")
    return value


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    """Serialize a run without leaking the Web host's attachment path."""
    result = deepcopy(run)
    artifact = result.get("artifact_snapshot") or {}
    local_path = str(artifact.get("local_path") or "")
    result = _scrub_local_path(result, local_path)
    # Give clients a stable name while retaining plan_json compatibility for
    # older frontend builds.
    if "plan_json" in result:
        result["plan"] = deepcopy(result["plan_json"])
    return result


async def _require_run(runtime, run_id: str) -> dict[str, Any]:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="部署任务不存在")
    try:
        return await runtime.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="部署任务不存在") from exc


def _schedule(rt, coroutine, *, label: str) -> None:
    async def runner() -> None:
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("部署后台任务失败: {}", label)

    task = asyncio.create_task(runner())
    rt.background_tasks.add(task)
    task.add_done_callback(rt.background_tasks.discard)


@router.post("", status_code=status.HTTP_201_CREATED)
async def api_create_deployment_run(payload: DeploymentRunCreate) -> dict[str, Any]:
    rt = get_runtime()
    if not rt.db or not rt.executor:
        raise HTTPException(status_code=503, detail="部署服务未初始化")
    session = await get_session(rt.db, payload.session_id, message_limit=1)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.get("type") != "chat":
        raise HTTPException(status_code=400, detail="只有聊天会话可以创建部署任务")

    request_id = payload.request_id.strip() or f"deploy_{uuid4().hex}"
    if not _REQUEST_ID_PATTERN.fullmatch(request_id):
        raise HTTPException(status_code=400, detail="request_id 格式无效")
    runtime = await _deployment_runtime(rt)
    existing = await runtime.storage.get_run_by_request(payload.session_id, request_id)
    if existing:
        existing_file = str(existing.get("artifact_snapshot", {}).get("file_id") or "")
        if existing.get("service_id") != payload.service_id or existing_file != payload.file_id:
            raise HTTPException(status_code=409, detail="request_id 已用于其他部署请求")
        return _public_run(await runtime.get(str(existing["id"])))

    try:
        service = _service_snapshot(rt, payload.service_id)
        artifact = await _artifact_snapshot(
            rt, session_id=payload.session_id, file_id=payload.file_id
        )
        run, created = await runtime.create(
            request_id=request_id,
            session_id=payload.session_id,
            service=service,
            artifact=artifact,
        )
        if created:
            # Prechecks are read-only and must finish before the browser can
            # approve the frozen plan. full_access is intentionally irrelevant.
            await runtime.prepare(str(run["id"]))
        run = await runtime.get(str(run["id"]))
    except DeploymentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _public_run(run)


@router.get("")
async def api_list_deployment_runs(
    session_id: str = Query(min_length=1, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    rt = get_runtime()
    if not rt.db:
        raise HTTPException(status_code=503, detail="部署服务未初始化")
    if not await get_session(rt.db, session_id, message_limit=1):
        raise HTTPException(status_code=404, detail="会话不存在")
    runtime = await _deployment_runtime(rt)
    runs = await runtime.list(session_id=session_id, limit=limit)
    return {"runs": [_public_run(item) for item in runs]}


@router.get("/{run_id}")
async def api_get_deployment_run(run_id: str) -> dict[str, Any]:
    rt = get_runtime()
    runtime = await _deployment_runtime(rt)
    return _public_run(await _require_run(runtime, run_id))


@router.post("/{run_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def api_confirm_deployment_plan(
    run_id: str, payload: DeploymentPlanConfirm
) -> dict[str, Any]:
    rt = get_runtime()
    runtime = await _deployment_runtime(rt)
    await _require_run(runtime, run_id)
    try:
        run = await runtime.confirm_plan(
            run_id, plan_hash=payload.plan_hash.lower(), confirmed_by="web_user"
        )
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _schedule(rt, runtime.execute(run_id), label=f"execute:{run_id}")
    return _public_run(await runtime.get(run_id))


@router.post("/{run_id}/cancel")
async def api_cancel_deployment_run(run_id: str) -> dict[str, Any]:
    rt = get_runtime()
    runtime = await _deployment_runtime(rt)
    await _require_run(runtime, run_id)
    try:
        run = await runtime.cancel(run_id)
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _public_run(await runtime.get(str(run["id"])))


@router.post("/{run_id}/rollback/confirm", status_code=status.HTTP_202_ACCEPTED)
async def api_confirm_deployment_rollback(
    run_id: str, payload: DeploymentRollbackConfirm | None = None
) -> dict[str, Any]:
    if payload is not None and not payload.confirmed:
        raise HTTPException(status_code=400, detail="必须明确确认回滚")
    rt = get_runtime()
    runtime = await _deployment_runtime(rt)
    await _require_run(runtime, run_id)
    try:
        await runtime.confirm_rollback(run_id, confirmed_by="web_user")
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _schedule(rt, runtime.rollback(run_id), label=f"rollback:{run_id}")
    return _public_run(await runtime.get(run_id))
