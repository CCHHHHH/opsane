"""Session lifecycle and persisted-session state REST endpoints."""
from __future__ import annotations

from pathlib import Path
import re

from fastapi import APIRouter, HTTPException

from shell_agent.core.context import SessionContext
from shell_agent.core.models import PendingCommand
from shell_agent.safety.classifier import classify_command
from shell_agent.storage.sessions import (
    create_session,
    get_session,
    list_sessions,
    rename_session,
    set_session_pinned,
    soft_delete_session,
)
from shell_agent.storage.session_files import soft_delete_all_session_files
from shell_agent.storage.file_transfers import (
    get_waiting_file_transfer,
    has_active_session_file_transfer,
)
from shell_agent.web.routes.file_transfers import _public_transfer
from shell_agent.storage.tasks import (
    get_task_events,
    get_session_tasks,
    update_task,
)
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import SessionCreate, SessionPinUpdate, SessionUpdate


router = APIRouter()


def _pending_key(session_id: str, channel: str) -> str:
    return f"{session_id}:{channel}"


def _pending_plan_key(session_id: str) -> str:
    return f"{session_id}:chat"


def _get_session_context(rt, session_id: str) -> SessionContext:
    if not hasattr(rt, "session_contexts"):
        rt.session_contexts = {}
    if session_id not in rt.session_contexts:
        rt.session_contexts[session_id] = SessionContext(session_id=session_id)
    return rt.session_contexts[session_id]


def _display_command(command: PendingCommand) -> str:
    return command.display_command or command.actual_command


def _command_preview_payload(
    rt,
    session_id: str,
    command: PendingCommand,
    channel: str,
) -> dict:
    risk = classify_command(command.actual_command)
    context = _get_session_context(rt, session_id)
    return {
        "session_id": session_id,
        "task_id": command.task_id,
        "turn_id": command.task_id if channel == "chat" else "",
        "channel": channel,
        "command": _display_command(command),
        "target": command.target,
        "cwd": context.get_cwd(command.target),
        "intent": command.intent,
        "explanation": command.explanation,
        "confirm_mode": command.confirm_mode,
        "policy_blocked": command.policy_blocked,
        "policy_block_reason": command.policy_block_reason,
        "requires_secondary_confirm": command.requires_secondary_confirm,
        "secondary_confirm_expected": command.secondary_confirm_expected,
        "secondary_confirm_label": command.secondary_confirm_label,
        "secondary_confirm_reason": command.secondary_confirm_reason,
        **risk.as_payload(),
    }


def _operation_plan_payload(plan: dict, active: bool = False) -> dict:
    return {
        "session_id": "",
        "turn_id": plan.get("turn_id") or "",
        "channel": "chat",
        "plan_id": plan.get("plan_id", ""),
        "intent": plan.get("intent", ""),
        "title": plan.get("title", ""),
        "goal": plan.get("goal", ""),
        "recommended_approach": plan.get("recommended_approach", ""),
        "impact": plan.get("impact", []),
        "risks": plan.get("risks", []),
        "rollback": plan.get("rollback", []),
        "verification": plan.get("verification", []),
        "steps": plan.get("steps", []),
        "active": active,
    }


def _session_pending_state(rt, session_id: str) -> dict:
    pending: dict[str, dict] = {}
    for channel in ("chat", "command"):
        command = getattr(rt, "pending_commands", {}).get(_pending_key(session_id, channel))
        if command:
            pending[channel] = _command_preview_payload(rt, session_id, command, channel)
    plan = getattr(rt, "pending_operation_plans", {}).get(_pending_plan_key(session_id))
    if plan:
        pending["operation_plan"] = _operation_plan_payload(plan, active=True)
    return pending


async def _session_task_state(rt, session_id: str) -> list[dict]:
    if not getattr(rt, "db", None):
        return []
    tasks = await get_session_tasks(rt.db, session_id, include_completed=False)
    for task in tasks:
        task["events"] = await get_task_events(rt.db, task["id"])
    return tasks


@router.get("/api/sessions")
async def api_list_sessions(type: str | None = None, q: str = "", limit: int = 100) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"sessions": []}
    sessions = await list_sessions(rt.db, session_type=type, query=q, limit=limit)
    return {"sessions": sessions}


@router.post("/api/sessions")
async def api_create_session(payload: SessionCreate) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"error": "数据库未初始化"}
    session_type = payload.type if payload.type in ("chat", "command") else "chat"
    session = await create_session(rt.db, session_type=session_type, title=payload.title)
    rt.session_contexts[session["id"]] = SessionContext(session_id=session["id"])
    return {"session": session}


@router.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str, message_limit: int = 0) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"error": "数据库未初始化"}
    safe_limit = max(0, int(message_limit or 0))
    session = await get_session(rt.db, session_id, message_limit=safe_limit or None)
    if not session:
        return {"error": f"会话 {session_id} 不存在"}
    session["pending"] = _session_pending_state(rt, session_id)
    waiting_transfer = await get_waiting_file_transfer(rt.db, session_id)
    if waiting_transfer:
        session["pending"]["file_transfer"] = _public_transfer(waiting_transfer)
    session["tasks"] = await _session_task_state(rt, session_id)
    return {"session": session}


@router.patch("/api/sessions/{session_id}")
async def api_update_session(session_id: str, payload: SessionUpdate) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"error": "数据库未初始化"}
    session = await rename_session(rt.db, session_id, payload.title)
    if not session:
        return {"error": f"会话 {session_id} 不存在或标题为空"}
    session.pop("messages", None)
    return {"session": session}


@router.put("/api/sessions/{session_id}/pin")
async def api_pin_session(session_id: str, payload: SessionPinUpdate) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"error": "数据库未初始化"}
    session = await set_session_pinned(rt.db, session_id, payload.pinned)
    if not session:
        return {"error": f"会话 {session_id} 不存在"}
    return {"session": session}


@router.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"error": "数据库未初始化"}
    if await has_active_session_file_transfer(rt.db, session_id):
        raise HTTPException(
            status_code=409,
            detail="会话存在待确认或正在执行的文件传输，暂时不能删除",
        )
    attached_files = await soft_delete_all_session_files(rt.db, session_id)
    ok = await soft_delete_session(rt.db, session_id)
    attachment_root = Path("data/session_files").resolve()
    for item in attached_files:
        raw_source = item.get("stored_path")
        if not raw_source:
            continue
        try:
            source = Path(str(raw_source)).resolve()
            if attachment_root != source and attachment_root not in source.parents:
                continue
            source.unlink(missing_ok=True)
            file_id = str(item.get("id") or "")
            if re.fullmatch(r"file_[A-Za-z0-9]+", file_id):
                preview_root = source.parent / ".previews"
                for sidecar in preview_root.glob(f"{file_id}*.pdf"):
                    if sidecar.parent.resolve() == preview_root.resolve():
                        sidecar.unlink(missing_ok=True)
        except OSError:
            pass
    for task in await get_session_tasks(rt.db, session_id, include_completed=False):
        await update_task(rt.db, task["id"], status="canceled", completed=True)
    rt.session_contexts.pop(session_id, None)
    for key in list(rt.pending_commands):
        if key.startswith(f"{session_id}:"):
            rt.pending_commands.pop(key, None)
    for key, task in list(getattr(rt, "running_tasks", {}).items()):
        if key.startswith(f"{session_id}:"):
            task.cancel()
            rt.running_tasks.pop(key, None)
    return {"ok": ok}
