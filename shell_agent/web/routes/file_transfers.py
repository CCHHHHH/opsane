"""Transfer files already attached to a chat session to an SSH target."""
from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
import re
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from loguru import logger

from shell_agent.core.context import SessionContext
from shell_agent.core.models import AuditRecord
from shell_agent.executors.ssh import normalize_upload_destination
from shell_agent.safety.audit import write_audit
from shell_agent.storage.file_transfers import (
    claim_file_transfer,
    confirm_file_transfer,
    create_file_transfer,
    finish_file_transfer,
    get_file_transfer,
    get_file_transfer_by_request,
    list_file_transfers,
)
from shell_agent.storage.session_files import get_session_file_for_session
from shell_agent.storage.sessions import add_session_message, get_session
from shell_agent.storage.tasks import add_task_event, update_task
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import (
    SessionFileTransferConfirm,
    SessionFileTransferCreate,
)
from shell_agent.web.ws.transport import manager


router = APIRouter()

MAX_TRANSFER_SIZE = 512 * 1024 * 1024
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _attachment_root() -> Path:
    return Path("data/session_files").resolve()


def _checked_local_file(item: dict) -> Path:
    path = Path(str(item.get("stored_path") or "")).resolve()
    root = _attachment_root()
    if root != path and root not in path.parents:
        raise HTTPException(status_code=404, detail="会话文件不存在")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="会话文件不存在")
    return path


def _file_digest(path: Path) -> tuple[int, str]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_TRANSFER_SIZE:
                raise ValueError("会话文件超过 512 MB 传输上限")
            digest.update(chunk)
    return size, digest.hexdigest()


def _server_fingerprint(server) -> str:
    value = f"{server.host}:{server.port}:{server.env}"
    return sha256(value.encode("utf-8")).hexdigest()


def _public_transfer(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "request_id": item.get("request_id"),
        "session_id": item.get("session_id"),
        "file_id": item.get("file_id"),
        "filename": item.get("file_name"),
        "file_name": item.get("file_name"),
        "target": item.get("target"),
        "target_env": item.get("target_env") or "",
        "remote_dir": item.get("remote_dir"),
        "remote_name": item.get("remote_name"),
        "remote_path": item.get("remote_path"),
        "overwrite": bool(item.get("overwrite")),
        "status": item.get("status"),
        "size": item.get("size"),
        "sha256": item.get("sha256"),
        "remote_size": item.get("remote_size"),
        "remote_sha256": item.get("remote_sha256") or "",
        "error": item.get("error") or "",
        "source": item.get("source") or "web",
        "turn_id": item.get("turn_id") or "",
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "completed_at": item.get("completed_at"),
    }


def _same_request(existing: dict, expected: dict) -> bool:
    return all(existing.get(key) == expected.get(key) for key in (
        "file_id", "target", "remote_dir", "remote_name", "remote_path"
    )) and bool(existing.get("overwrite")) == bool(expected.get("overwrite"))


def _safe_error(exc: BaseException, local_path: Path | None = None) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if local_path:
        message = message.replace(str(local_path), "会话文件")
    return message[:1000]


async def _write_transfer_audit(
    rt,
    transfer: dict,
    *,
    success: bool,
    duration_ms: int,
    stdout: str = "",
    stderr: str = "",
) -> None:
    server = rt.executor.resolve_server(str(transfer["target"]))
    record = AuditRecord(
        command=(
            f"upload session-file:{transfer['file_id']} "
            f"-> {transfer['remote_path']}"
        ),
        target=str(transfer["target"]),
        target_env=server.env,
        executor="sftp",
        executed=True,
        source=str(transfer.get("source") or "web"),
        caller="web_user",
        session_id=str(transfer["session_id"]),
        user_confirmed=True,
        exit_code=0 if success else 1,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
    )
    try:
        await write_audit(rt.db, record)
    except Exception as exc:  # pragma: no cover - audit failure must not mask result
        logger.error(f"文件传输审计写入失败: {exc}")


async def _persist_artifact_upload(rt, transfer: dict) -> None:
    session_id = str(transfer["session_id"])
    artifact = {
        "transfer_id": transfer["id"],
        "source_file_id": transfer["file_id"],
        "target": transfer["target"],
        "filename": transfer["file_name"],
        "remote_filename": transfer["remote_name"],
        "remote_dir": transfer["remote_dir"],
        "remote_path": transfer["remote_path"],
        "size": transfer["remote_size"],
        "sha256": transfer["remote_sha256"],
        "status": "success",
    }
    if session_id not in rt.session_contexts:
        rt.session_contexts[session_id] = SessionContext(session_id=session_id)
    rt.session_contexts[session_id].add_artifact_upload(artifact)
    await add_session_message(
        rt.db,
        session_id=session_id,
        role="system",
        msg_type="artifact_upload",
        content=f"会话文件已传输: {transfer['target']}:{transfer['remote_path']}",
        payload={
            "turn_id": transfer.get("turn_id") or "",
            "artifact": artifact,
            "transfer": _public_transfer(transfer),
        },
    )
    await manager.send({
        "type": "artifact_upload",
        "session_id": session_id,
        "turn_id": transfer.get("turn_id") or "",
        "content": f"会话文件已传输到 {transfer['target']}:{transfer['remote_path']}",
        "artifact": artifact,
    })


async def _persist_failed_artifact_upload(rt, transfer: dict, error: str) -> None:
    """Persist a visible failure without promoting it to session artifacts."""
    session_id = str(transfer["session_id"])
    artifact = {
        "transfer_id": transfer["id"],
        "source_file_id": transfer["file_id"],
        "file_id": transfer["file_id"],
        "filename": transfer["file_name"],
        "target": transfer["target"],
        "remote_path": transfer["remote_path"],
        "status": transfer["status"],
        "error": error,
    }
    await add_session_message(
        rt.db,
        session_id=session_id,
        role="system",
        msg_type="artifact_upload",
        content=f"会话文件传输失败: {error}",
        payload={
            "turn_id": transfer.get("turn_id") or "",
            "artifact": artifact,
            "transfer": _public_transfer(transfer),
        },
    )
    await manager.send({
        "type": "artifact_upload",
        "session_id": session_id,
        "turn_id": transfer.get("turn_id") or "",
        "content": error,
        "artifact": artifact,
    })


async def _update_transfer_turn(
    rt,
    transfer: dict,
    *,
    status: str,
    label: str,
    completed: bool = False,
) -> None:
    """Persist and broadcast the owning conversational turn, when present."""
    turn_id = str(transfer.get("turn_id") or "")
    if not turn_id or not getattr(rt, "db", None):
        return
    await update_task(rt.db, turn_id, status=status, completed=completed)
    payload = {
        "turn_id": turn_id,
        "session_id": str(transfer["session_id"]),
        "channel": "chat",
        "status": status,
        "label": label,
        "active": not completed,
        "transfer_id": str(transfer["id"]),
    }
    await add_task_event(
        rt.db,
        turn_id,
        str(transfer["session_id"]),
        "chat",
        "turn_state",
        status=status,
        content=label,
        payload=payload,
    )
    await manager.send({"type": "turn_state", **payload})


async def _run_transfer(rt, transfer_id: str, session_id: str, local_path: Path) -> None:
    claimed, transfer = await claim_file_transfer(rt.db, transfer_id, session_id)
    if not claimed or not transfer:
        return
    start = time.monotonic()
    try:
        result = await rt.executor.upload_file_verified(
            target=str(transfer["target"]),
            local_path=local_path,
            remote_dir=str(transfer["remote_dir"]),
            remote_name=str(transfer["remote_name"]),
            overwrite=bool(transfer["overwrite"]),
            expected_size=int(transfer["size"]),
            expected_sha256=str(transfer["sha256"]),
            operation_id=str(transfer["id"]),
            timeout=max(int(rt.config.ssh.default_timeout), 300),
        )
        completed = await finish_file_transfer(
            rt.db,
            transfer_id,
            status="success",
            remote_size=result.size,
            remote_sha256=result.sha256,
        )
        if not completed:  # pragma: no cover - defensive database invariant
            return
        duration_ms = int((time.monotonic() - start) * 1000)
        await _write_transfer_audit(
            rt,
            completed,
            success=True,
            duration_ms=duration_ms,
            stdout=(
                f"remote={completed['remote_path']} size={result.size} "
                f"sha256={result.sha256}"
            ),
        )
        try:
            await _persist_artifact_upload(rt, completed)
        except Exception as exc:  # keep the durable transfer/task terminal
            logger.error(f"文件传输成功结果写入会话失败: {exc}")
        await _update_transfer_turn(
            rt, completed, status="completed", label="文件上传完成", completed=True
        )
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            error = "文件传输已中断"
            status = "interrupted"
        else:
            error = _safe_error(exc, local_path)
            status = "failed"
        completed = await finish_file_transfer(
            rt.db,
            transfer_id,
            status=status,
            error=error,
        )
        if completed:
            await _write_transfer_audit(
                rt,
                completed,
                success=False,
                duration_ms=int((time.monotonic() - start) * 1000),
                stderr=error,
            )
            try:
                await _persist_failed_artifact_upload(rt, completed, error)
            except Exception as persist_exc:  # keep the failed task terminal
                logger.error(f"文件传输失败结果写入会话失败: {persist_exc}")
            await _update_transfer_turn(
                rt,
                completed,
                status="failed" if status == "failed" else "canceled",
                label=error,
                completed=True,
            )
        if isinstance(exc, asyncio.CancelledError):
            raise
        logger.exception(
            f"会话文件传输失败: transfer={transfer_id} target={transfer.get('target')}"
        )


def _schedule_transfer(rt, transfer: dict, local_path: Path) -> None:
    task = asyncio.create_task(
        _run_transfer(rt, str(transfer["id"]), str(transfer["session_id"]), local_path)
    )
    rt.background_tasks.add(task)
    task.add_done_callback(rt.background_tasks.discard)


async def prepare_file_transfer(
    rt,
    *,
    session_id: str,
    file_id: str,
    target: str,
    remote_dir: str,
    remote_name: str = "",
    overwrite: bool = False,
    request_id: str = "",
    initial_status: str = "pending",
    source: str = "web",
    turn_id: str = "",
) -> tuple[dict, bool, Path]:
    """Validate, hash and durably create one session-scoped transfer."""
    if not rt.db or not rt.executor:
        raise HTTPException(status_code=503, detail="文件传输服务未初始化")
    session = await get_session(rt.db, session_id, message_limit=1)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.get("type") != "chat":
        raise HTTPException(status_code=400, detail="只有聊天会话可以传输文件")

    canonical = rt.executor.resolve_server_alias(target.strip())
    if canonical is None:
        raise HTTPException(status_code=400, detail=f"未知服务器别名: {target}")
    server = rt.executor.resolve_server(canonical)
    normalized_request_id = request_id.strip() or f"req_{uuid4().hex}"
    if not _REQUEST_ID_PATTERN.fullmatch(normalized_request_id):
        raise HTTPException(status_code=400, detail="request_id 格式无效")

    item = await get_session_file_for_session(rt.db, session_id, file_id)
    if not item:
        raise HTTPException(status_code=404, detail="会话文件不存在")
    normalized_name = remote_name.strip() or str(
        item.get("original_name") or "file.bin"
    )
    try:
        normalized_dir, normalized_name, remote_path = normalize_upload_destination(
            remote_dir or "/tmp/shell-agent-uploads", normalized_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    expected = {
        "file_id": file_id,
        "target": canonical,
        "remote_dir": normalized_dir,
        "remote_name": normalized_name,
        "remote_path": remote_path,
        "overwrite": overwrite,
    }
    existing = await get_file_transfer_by_request(
        rt.db, session_id, normalized_request_id
    )
    if existing:
        if not _same_request(existing, expected):
            raise HTTPException(
                status_code=409,
                detail="request_id 已用于其他文件传输请求",
            )
        return existing, False, _checked_local_file(item)

    local_path = _checked_local_file(item)
    try:
        size, actual_sha256 = await asyncio.to_thread(_file_digest, local_path)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    if size <= 0:
        raise HTTPException(status_code=400, detail="会话文件为空")
    if size != int(item.get("size") or 0) or actual_sha256 != str(
        item.get("sha256") or ""
    ):
        raise HTTPException(status_code=409, detail="会话文件内容与上传记录不一致")

    transfer, created = await create_file_transfer(
        rt.db,
        request_id=normalized_request_id,
        session_id=session_id,
        file_id=file_id,
        file_name=str(item.get("original_name") or normalized_name),
        target=canonical,
        target_env=server.env,
        target_fingerprint=_server_fingerprint(server),
        remote_dir=normalized_dir,
        remote_name=normalized_name,
        remote_path=remote_path,
        overwrite=overwrite,
        size=size,
        sha256=actual_sha256,
        initial_status=initial_status,
        source=source,
        turn_id=turn_id,
    )
    if not created and not _same_request(transfer, expected):
        raise HTTPException(status_code=409, detail="request_id 已用于其他文件传输请求")
    return transfer, created, local_path


async def resolve_file_transfer_confirmation(
    rt,
    *,
    session_id: str,
    transfer_id: str,
    confirmed: bool,
) -> tuple[dict, bool]:
    """Resolve one waiting confirmation and schedule at most one upload."""
    if not rt.db or not rt.executor:
        raise HTTPException(status_code=503, detail="文件传输服务未初始化")
    current = await get_file_transfer(rt.db, transfer_id, session_id=session_id)
    if not current:
        raise HTTPException(status_code=404, detail="待确认文件传输不存在")
    if current.get("status") != "waiting_confirm":
        current_status = str(current.get("status") or "")
        same_decision = (
            (confirmed and current_status != "cancelled")
            or (not confirmed and current_status == "cancelled")
        )
        if not same_decision:
            raise HTTPException(status_code=409, detail="文件传输确认状态已确定")
        return current, True

    local_path: Path | None = None
    if confirmed:
        try:
            server = rt.executor.resolve_server(str(current["target"]))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        frozen_env = str(current.get("target_env") or "")
        if frozen_env and server.env != frozen_env:
            raise HTTPException(
                status_code=409,
                detail="目标服务器环境已变化，请重新发起文件传输",
            )
        frozen_fingerprint = str(current.get("target_fingerprint") or "")
        if frozen_fingerprint and _server_fingerprint(server) != frozen_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="目标服务器配置已变化，请重新发起文件传输",
            )
        item = await get_session_file_for_session(
            rt.db, session_id, str(current["file_id"])
        )
        if not item:
            raise HTTPException(status_code=409, detail="会话文件已不存在")
        local_path = _checked_local_file(item)

    changed, transfer = await confirm_file_transfer(
        rt.db,
        transfer_id,
        session_id,
        confirmed=confirmed,
    )
    if not transfer:
        raise HTTPException(status_code=404, detail="待确认文件传输不存在")
    if not changed:
        return transfer, True
    if confirmed:
        assert local_path is not None
        await _update_transfer_turn(
            rt, transfer, status="executing", label="正在上传文件"
        )
        _schedule_transfer(rt, transfer, local_path)
    else:
        await _update_transfer_turn(
            rt, transfer, status="canceled", label="已取消文件上传", completed=True
        )
    return transfer, False


@router.post("/api/sessions/{session_id}/files/{file_id}/transfers", status_code=202)
async def api_create_file_transfer(
    session_id: str,
    file_id: str,
    payload: SessionFileTransferCreate,
) -> dict:
    rt = get_runtime()
    transfer, created, local_path = await prepare_file_transfer(
        rt,
        session_id=session_id,
        file_id=file_id,
        target=payload.target,
        remote_dir=payload.remote_dir,
        remote_name=payload.remote_name,
        overwrite=payload.overwrite,
        request_id=payload.request_id,
    )
    if created:
        _schedule_transfer(rt, transfer, local_path)
    return {
        "ok": True,
        "transfer": _public_transfer(transfer),
        "message": (
            "文件传输任务已提交"
            if created else "相同文件传输请求已受理"
        ),
    }


@router.post(
    "/api/sessions/{session_id}/file-transfers/{transfer_id}/confirm",
    status_code=202,
)
async def api_confirm_file_transfer(
    session_id: str,
    transfer_id: str,
    payload: SessionFileTransferConfirm,
) -> dict:
    rt = get_runtime()
    transfer, duplicate = await resolve_file_transfer_confirmation(
        rt,
        session_id=session_id,
        transfer_id=transfer_id,
        confirmed=payload.confirmed,
    )
    return {
        "ok": True,
        "accepted": True,
        "duplicate": duplicate,
        "request_id": payload.request_id,
        "transfer": _public_transfer(transfer),
        "message": "确认请求已受理" if payload.confirmed else "文件传输已取消",
    }


@router.get("/api/sessions/{session_id}/file-transfers")
async def api_list_file_transfers(session_id: str, limit: int = 100) -> dict:
    rt = get_runtime()
    if not rt.db:
        raise HTTPException(status_code=503, detail="数据库未初始化")
    session = await get_session(rt.db, session_id, message_limit=1)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    transfers = await list_file_transfers(rt.db, session_id, limit=limit)
    return {"transfers": [_public_transfer(item) for item in transfers]}
