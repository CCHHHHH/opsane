"""API 路由：REST + WebSocket"""
from __future__ import annotations

import asyncio
from datetime import datetime
import re
from typing import Optional
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from loguru import logger

from shell_agent.core.models import (
    AuditRecord, ConfirmMode, ExecutionResult, PendingCommand,
)
from shell_agent.core.context import SessionContext, compact_text
from shell_agent.llm.adapter import LLMAdapter
from shell_agent.knowledge import KnowledgeResolver, learn_from_task
from shell_agent.safety.audit import write_audit
from shell_agent.safety.classifier import RiskLevel, classify_command
from shell_agent.safety.policy import evaluate_environment_policy
from shell_agent.skills import load_template_skills, match_template_skill
from shell_agent.skills.discovery import discover_skill_candidates
from shell_agent.storage.sessions import (
    add_session_message,
    ensure_session,
    get_session,
    maybe_update_title_from_user_message,
)
from shell_agent.storage.memories import (
    upsert_memory,
)
from shell_agent.storage.file_transfers import get_waiting_file_transfer
from shell_agent.storage.tasks import (
    add_task_event,
    claim_task_confirmation,
    create_task,
    get_task,
    get_task_events,
    get_session_tasks,
    update_task,
)
from shell_agent.web.runtime import get_runtime
from shell_agent.web.routes.audit import router as audit_router
from shell_agent.web.routes.config import router as config_router
from shell_agent.web.routes.credentials import router as credentials_router
from shell_agent.web.routes.inventory import router as inventory_router
from shell_agent.web.routes.knowledge import router as knowledge_router
from shell_agent.web.routes.memories import router as memories_router
from shell_agent.web.routes.safety import router as safety_router
from shell_agent.web.routes.sessions import (
    _restore_pending_command_from_task,
    _session_pending_state,
    _session_task_state,
    router as sessions_router,
)
from shell_agent.web.routes.session_files import router as session_files_router
from shell_agent.web.routes.file_transfers import (
    _public_transfer,
    prepare_file_transfer,
    resolve_file_transfer_confirmation,
    router as file_transfers_router,
)
from shell_agent.web.routes.deployment_runs import router as deployment_runs_router
from shell_agent.web.routes.skills import router as skills_router
from shell_agent.web.routes.skill_candidates import router as skill_candidates_router
from shell_agent.web.routes.state import router as state_router
from shell_agent.web.schemas import (
    ChatRequest,
    CommandRequest,
    ConfirmRequest,
    ConfigUpdate,
    CredentialUpsert,
    MemoryCreate,
    SafetyClassifyRequest,
    SafetyConfigUpdate,
    ServerCreate,
    ServiceProfileUpsert,
    SessionCreate,
    SessionUpdate,
    SkillYamlUpdate,
)
from shell_agent.web.ws.transport import (
    _SEND_SESSION_ID,
    _SEND_TURN_ID,
    ConnectionManager,
    _send,
    manager,
)
from shell_agent.web.ws.completion import (
    _common_prefix,
    _complete_builtin_command,
    _complete_remote_path,
    _completion_token,
    _unique,
)
from shell_agent.web.ws.plans import (
    _actual_command_for_risk,
    _as_str_list,
    _get_pending_operation_plan,
    _is_operation_plan_result,
    _normalize_operation_plan,
    _operation_plan_from_command_result,
    _operation_plan_payload,
    _pending_plan_key,
    _planned_steps_from_result,
    _should_force_operation_plan,
    _target_from_ssh_command,
    _unique_strings,
)
from shell_agent.web.ws.session_state import (
    _apply_client_cwd,
    _apply_direct_shell_state,
    _default_target_alias,
    _display_command,
    _get_hydrated_session_context,
    _get_session_context,
    _normalize_direct_command_input,
    _update_direct_cwd_after_execution,
)
from shell_agent.web.ws.context_summary import _llm_context_history
from shell_agent.web.ws.file_transfer_intent import (
    resolve_conversational_file_transfer,
)


router = APIRouter()
router.include_router(state_router)
router.include_router(memories_router)
router.include_router(audit_router)
router.include_router(credentials_router)
router.include_router(config_router)
router.include_router(inventory_router)
router.include_router(knowledge_router)
router.include_router(skills_router)
router.include_router(skill_candidates_router)
router.include_router(safety_router)
router.include_router(sessions_router)
router.include_router(session_files_router)
router.include_router(file_transfers_router)
router.include_router(deployment_runs_router)

# ========== 聊天接口（WebSocket）==========

@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    """聊天 WebSocket：双向通信"""
    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=1008, reason="不允许的 WebSocket Origin")
        return
    await manager.connect(websocket)
    rt = get_runtime()
    session_id = f"ws_{datetime.now().strftime('%H%M%S')}"

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "chat")
            confirm_mode = data.get("confirm_mode", ConfirmMode.INTERACTIVE.value)
            active_session_id = data.get("session_id") or session_id
            token = _SEND_SESSION_ID.set(active_session_id)
            try:
                manager.subscribe(websocket, active_session_id)
                if msg_type == "subscribe":
                    await _send_session_sync(
                        websocket,
                        rt,
                        active_session_id,
                        data.get("channel", "chat"),
                    )
                elif msg_type == "chat":
                    _start_chat_turn(
                        websocket,
                        rt,
                        active_session_id,
                        data.get("message", ""),
                        confirm_mode,
                        data.get("target", ""),
                    )
                elif msg_type == "confirm":
                    await _handle_confirm(
                        websocket,
                        rt,
                        active_session_id,
                        data.get("confirmed", False),
                        data.get("channel", "chat"),
                        data.get("task_id", ""),
                        data.get("secondary_confirm_value", ""),
                        data.get("operation_id", ""),
                        data.get("request_id", ""),
                    )
                elif msg_type == "plan_confirm":
                    await _handle_plan_confirm(
                        websocket,
                        rt,
                        active_session_id,
                        data.get("plan_id", ""),
                        data.get("confirmed", False),
                    )
                elif msg_type == "plan_adjust":
                    await _handle_plan_adjust(
                        websocket,
                        rt,
                        active_session_id,
                        data.get("plan_id", ""),
                        data.get("instruction", ""),
                    )
                elif msg_type == "file_transfer_confirm":
                    await _handle_file_transfer_confirm(
                        websocket,
                        rt,
                        active_session_id,
                        data.get("transfer_id", ""),
                        data.get("confirmed", False),
                        data.get("request_id", ""),
                    )
                elif msg_type == "command":
                    await _handle_direct_command(
                        websocket,
                        rt,
                        active_session_id,
                        data.get("command", ""),
                        confirm_mode,
                        data.get("target", ""),
                        data.get("cwd", ""),
                    )
                elif msg_type == "complete":
                    await _handle_completion(
                        websocket,
                        rt,
                        active_session_id,
                        data.get("command", ""),
                        int(data.get("cursor", 0)),
                        data.get("target", ""),
                        data.get("cwd", ""),
                        data.get("request_id", ""),
                        data.get("input_id", ""),
                    )
                elif msg_type == "cancel":
                    await _handle_cancel(
                        websocket,
                        rt,
                        active_session_id,
                        data.get("channel", "command"),
                    )
            finally:
                _SEND_SESSION_ID.reset(token)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.exception(f"WebSocket 异常: {e}")
        manager.disconnect(websocket)


async def _send_session_sync(
    websocket: WebSocket,
    rt,
    session_id: str,
    channel: str = "chat",
) -> None:
    """Replay one authoritative session snapshot after connect or refresh."""
    messages: list[dict] = []
    if getattr(rt, "db", None):
        session = await get_session(rt.db, session_id, message_limit=200)
        if session:
            messages = list(session.get("messages") or [])
    pending = await _session_pending_state(rt, session_id)
    if getattr(rt, "db", None):
        waiting_transfer = await get_waiting_file_transfer(rt.db, session_id)
        if waiting_transfer:
            pending["file_transfer"] = _public_transfer(waiting_transfer)
    await _send(
        websocket,
        "session_sync",
        broadcast=False,
        session_id=session_id,
        channel=channel if channel in {"chat", "command"} else "chat",
        messages=messages,
        pending=pending,
        tasks=await _session_task_state(rt, session_id),
    )


async def _create_chat_turn(rt, session_id: str, message: str, confirm_mode: str) -> str:
    """Create the authoritative turn record for one Opsane reply."""
    if not getattr(rt, "db", None):
        return f"turn_{uuid4().hex[:12]}"
    task = await create_task(
        rt.db,
        session_id=session_id,
        channel="chat",
        title=message.strip()[:80],
        total_steps=1,
        confirm_mode=confirm_mode,
    )
    turn_id = task["id"]
    await update_task(rt.db, turn_id, status="running")
    return turn_id


async def _send_turn_state(
    websocket: WebSocket,
    rt,
    session_id: str,
    turn_id: str,
    status: str,
    label: str,
    completed: bool = False,
) -> None:
    """Send and persist the authoritative state for a chat turn."""
    if not turn_id:
        return
    if getattr(rt, "db", None):
        await update_task(
            rt.db,
            turn_id,
            status=status,
            completed=completed or status in {"completed", "failed", "canceled", "blocked", "timeout"},
        )
        await add_task_event(
            rt.db,
            turn_id,
            session_id,
            "chat",
            "turn_state",
            status=status,
            content=label,
            payload={
                "turn_id": turn_id,
                "session_id": session_id,
                "channel": "chat",
                "status": status,
                "label": label,
                "active": not completed and status not in {"completed", "failed", "canceled", "blocked", "timeout"},
            },
        )
    await _send(
        websocket,
        "turn_state",
        turn_id=turn_id,
        channel="chat",
        status=status,
        label=label,
        active=not completed and status not in {"completed", "failed", "canceled", "blocked", "timeout"},
    )


async def _terminalize_chat_turn(
    websocket: WebSocket,
    rt,
    session_id: str,
    turn_id: str,
    status: str,
    label: str,
) -> None:
    """Best-effort terminal state write; persistence must survive UI disconnects."""
    try:
        await _send_turn_state(
            websocket,
            rt,
            session_id,
            turn_id,
            status,
            label,
            completed=True,
        )
    except Exception as exc:
        logger.warning(f"发送任务终态失败: task={turn_id} status={status} error={exc}")
        if getattr(rt, "db", None):
            await update_task(rt.db, turn_id, status=status, completed=True)


async def _try_conversational_file_transfer(
    websocket: WebSocket,
    rt,
    session_id: str,
    message: str,
    turn_id: str,
) -> bool:
    """Create a durable preview for an unambiguous natural-language upload."""
    resolution = await resolve_conversational_file_transfer(rt, session_id, message)
    if not resolution.attempted:
        return False
    if not resolution.intent:
        content = resolution.clarification or "请补充文件、服务器和远端绝对目录。"
        await _send(websocket, "agent", content=content)
        await _persist_session_message(
            rt, session_id, "agent", "agent", content=content, session_type="chat"
        )
        await _send_turn_state(
            websocket, rt, session_id, turn_id, "completed", "等待补充信息", completed=True
        )
        return True

    intent = resolution.intent
    try:
        transfer, _created, _local_path = await prepare_file_transfer(
            rt,
            session_id=session_id,
            file_id=intent.file_id,
            target=intent.target,
            remote_dir=intent.remote_dir,
            remote_name=intent.file_name,
            overwrite=intent.overwrite,
            request_id=f"chat_{turn_id}",
            initial_status="waiting_confirm",
            source="chat",
            turn_id=turn_id,
        )
    except HTTPException as exc:
        content = str(exc.detail)
        await _send(websocket, "agent", content=content)
        await _persist_session_message(
            rt, session_id, "agent", "agent", content=content, session_type="chat"
        )
        await _send_turn_state(
            websocket, rt, session_id, turn_id, "failed", "文件传输预览失败", completed=True
        )
        return True

    payload = {
        "session_id": session_id,
        "turn_id": turn_id,
        "channel": "chat",
        "transfer": _public_transfer(transfer),
        "requires_confirmation": True,
        "confirm_mode": "interactive",
    }
    content = (
        f"准备将 {intent.file_name} 上传到 "
        f"{transfer['target']}:{transfer['remote_path']}，确认后开始传输。"
    )
    if getattr(rt, "db", None):
        await update_task(rt.db, turn_id, status="waiting_confirm")
        await add_task_event(
            rt.db,
            turn_id,
            session_id,
            "chat",
            "file_transfer_preview",
            status="waiting_confirm",
            content=content,
            payload=payload,
        )
    await _send(websocket, "file_transfer_preview", **payload)
    await _persist_session_message(
        rt,
        session_id,
        "assistant",
        "file_transfer_preview",
        content=content,
        payload=payload,
        session_type="chat",
    )
    await _send_turn_state(
        websocket, rt, session_id, turn_id, "waiting_confirm", "等待确认文件上传"
    )
    return True


async def _handle_file_transfer_confirm(
    websocket: WebSocket,
    rt,
    session_id: str,
    transfer_id: str,
    confirmed: bool,
    request_id: str = "",
) -> None:
    """Resolve a scoped file-transfer confirmation with idempotent feedback."""
    try:
        transfer, duplicate = await resolve_file_transfer_confirmation(
            rt,
            session_id=session_id,
            transfer_id=transfer_id.strip(),
            confirmed=bool(confirmed),
        )
    except HTTPException as exc:
        await _send(
            websocket,
            "file_transfer_confirm_ack",
            transfer_id=transfer_id,
            request_id=request_id,
            confirmed=bool(confirmed),
            accepted=False,
            duplicate=False,
            status="not_found" if exc.status_code == 404 else "conflict",
            content=str(exc.detail),
        )
        return
    await _send(
        websocket,
        "file_transfer_confirm_ack",
        transfer_id=transfer_id,
        request_id=request_id,
        confirmed=bool(confirmed),
        accepted=True,
        duplicate=duplicate,
        status=str(transfer.get("status") or ""),
        content="确认请求已受理" if confirmed else "文件传输已取消",
        transfer=_public_transfer(transfer),
    )


async def _handle_chat_message(
    websocket: WebSocket,
    rt,
    session_id: str,
    message: str,
    confirm_mode: str = ConfirmMode.INTERACTIVE.value,
    target: str = "",
) -> None:
    """处理聊天消息"""
    if not message.strip():
        return

    await _ensure_session(rt, session_id, "chat", title=message.strip()[:32])
    turn_id = await _create_chat_turn(rt, session_id, message, confirm_mode)
    owner_key = _chat_turn_key(session_id)
    if not hasattr(rt, "running_task_ids"):
        rt.running_task_ids = {}
    rt.running_task_ids[owner_key] = turn_id
    turn_token = _SEND_TURN_ID.set(turn_id)
    try:
        await _handle_chat_message_with_turn(
            websocket,
            rt,
            session_id,
            message,
            confirm_mode,
            target,
            turn_id,
        )
    except asyncio.CancelledError:
        await _terminalize_chat_turn(
            websocket,
            rt,
            session_id,
            turn_id,
            "canceled",
            "已停止",
        )
        await _send(websocket, "execution_status", status="canceled", content="已停止", channel="chat")
        await _send(websocket, "system", content="已停止当前回复", channel="chat")
        await _persist_session_message(
            rt,
            session_id,
            "system",
            "system",
            content="已停止当前回复",
            payload={"channel": "chat", "turn_id": turn_id},
            session_type="chat",
        )
        raise
    except Exception as exc:
        logger.exception(f"聊天任务异常: task={turn_id} error={exc}")
        await _terminalize_chat_turn(
            websocket,
            rt,
            session_id,
            turn_id,
            "failed",
            "任务异常",
        )
        content = f"任务处理失败: {exc}"
        await _send(websocket, "system", content=content, channel="chat")
        await _persist_session_message(
            rt,
            session_id,
            "system",
            "system",
            content=content,
            payload={"channel": "chat", "turn_id": turn_id},
            session_type="chat",
        )
    finally:
        _SEND_TURN_ID.reset(turn_token)
        if getattr(rt, "running_task_ids", {}).get(owner_key) == turn_id:
            rt.running_task_ids.pop(owner_key, None)


async def _handle_chat_message_with_turn(
    websocket: WebSocket,
    rt,
    session_id: str,
    message: str,
    confirm_mode: str,
    target: str,
    turn_id: str,
) -> None:
    """处理单轮聊天消息。调用方负责设置当前 turn_id。"""
    context = await _get_hydrated_session_context(rt, session_id)
    context.set_target(target)
    context.add_user_message(message)
    await _send_turn_state(websocket, rt, session_id, turn_id, "thinking", "正在思考")
    await _send(websocket, "user_message", content=message)
    await _persist_session_message(
        rt,
        session_id,
        "user",
        "user_message",
        content=message,
        payload={"turn_id": turn_id},
        session_type="chat",
    )
    await _maybe_update_session_title(websocket, rt, session_id, "chat", message)

    pending = await _get_blocking_chat_pending(rt, session_id)
    if pending:
        content = _format_pending_command_block_message(pending)
        context.add_event("待确认命令阻塞", content)
        await _send_turn_state(websocket, rt, session_id, turn_id, "blocked", "已有命令等待确认", completed=True)
        await _send(websocket, "system", content=content)
        await _persist_session_message(
            rt,
            session_id,
            "system",
            "system",
            content=content,
            session_type="chat",
        )
        return

    waiting_transfer = (
        await get_waiting_file_transfer(rt.db, session_id)
        if getattr(rt, "db", None)
        else None
    )
    if waiting_transfer:
        content = (
            f"已有文件传输等待确认：{waiting_transfer['file_name']} → "
            f"{waiting_transfer['target']}:{waiting_transfer['remote_path']}。"
            "请先确认或取消该传输。"
        )
        context.add_event("待确认文件传输阻塞", content)
        await _send_turn_state(
            websocket,
            rt,
            session_id,
            turn_id,
            "blocked",
            "已有文件传输等待确认",
            completed=True,
        )
        await _send(websocket, "system", content=content)
        await _persist_session_message(
            rt, session_id, "system", "system", content=content, session_type="chat"
        )
        return

    if await _try_conversational_file_transfer(
        websocket, rt, session_id, message, turn_id
    ):
        return

    manual_memory = _parse_manual_memory(message, rt)
    if manual_memory and getattr(rt, "db", None):
        memory = await upsert_memory(
            rt.db,
            **manual_memory,
            source_session_id=session_id,
            source="manual_chat",
        )
        content = _format_memory_saved(memory)
        context.add_event("全局记忆", content)
        await _send(websocket, "agent", content=content)
        await _persist_session_message(
            rt,
            session_id,
            "agent",
            "agent",
            content=content,
            payload={"memory": memory},
            session_type="chat",
        )
        await _send_turn_state(websocket, rt, session_id, turn_id, "completed", "已回复", completed=True)
        return

    if await _try_skill_history_discovery(
        websocket, rt, session_id, message, turn_id
    ):
        return

    if _is_artifact_deploy_request(message) and not context.latest_artifact():
        content = (
            "当前会话还没有已传到服务器的文件。请先在聊天中上传文件，"
            "再从文件侧栏选择“传到服务器”，传输完成后即可继续部署。"
        )
        await _send(websocket, "system", content=content)
        await _persist_session_message(
            rt, session_id, "system", "system", content=content, session_type="chat"
        )
        await _send_turn_state(websocket, rt, session_id, turn_id, "completed", "已回复", completed=True)
        return

    if await _try_template_skill(
        websocket=websocket,
        rt=rt,
        session_id=session_id,
        message=message,
        confirm_mode=confirm_mode,
        target=target,
        context=context,
    ):
        return

    if not rt.llm:
        await _send_turn_state(websocket, rt, session_id, turn_id, "failed", "LLM 未配置", completed=True)
        await _send(websocket, "system", content="LLM 未配置")
        await _persist_session_message(
            rt, session_id, "system", "system", content="LLM 未配置", session_type="chat"
        )
        return

    await _send_turn_state(websocket, rt, session_id, turn_id, "thinking", "正在生成命令")
    await _send(websocket, "system", content="正在生成命令...", transient=True)

    try:
        memory_history = await _memory_history_for_input(rt, message, target)
        context_history = await _llm_context_history(rt, session_id, query=message)
        result = await rt.llm.generate_command(
            message,
            history=[*memory_history, *context_history],
        )
    except Exception as e:
        await _send_turn_state(websocket, rt, session_id, turn_id, "failed", "LLM 调用失败", completed=True)
        await _send(websocket, "system", content=f"LLM 调用失败: {e}")
        await _persist_session_message(
            rt,
            session_id,
            "system",
            "system",
            content=f"LLM 调用失败: {e}",
            session_type="chat",
        )
        return

    if isinstance(result, str):
        await _send(websocket, "agent", content=result)
        await _persist_session_message(
            rt, session_id, "agent", "agent", content=result, session_type="chat"
        )
        await _send_turn_state(websocket, rt, session_id, turn_id, "completed", "已回复", completed=True)
        return

    if _is_operation_plan_result(result):
        await _send_operation_plan(
            websocket=websocket,
            rt=rt,
            session_id=session_id,
            result=result,
            user_input=message,
            confirm_mode=confirm_mode,
        )
        return

    planned_steps = _planned_steps_from_result(result)
    if _should_force_operation_plan(result, planned_steps):
        await _send_operation_plan(
            websocket=websocket,
            rt=rt,
            session_id=session_id,
            result=_operation_plan_from_command_result(result, planned_steps, message),
            user_input=message,
            confirm_mode=confirm_mode,
        )
        return
    first_step = planned_steps[0] if planned_steps else {}
    command_str = first_step.get("command") or result.get("command", "")
    intent = first_step.get("intent") or result.get("intent", "")
    explanation = first_step.get("explanation") or result.get("explanation", "")
    if intent:
        await _send(websocket, "agent", content=intent)
        await _persist_session_message(
            rt, session_id, "agent", "agent", content=intent, session_type="chat"
        )

    if not command_str:
        await _send_turn_state(websocket, rt, session_id, turn_id, "failed", "未生成命令", completed=True)
        await _send(websocket, "system", content="LLM 未生成命令")
        await _persist_session_message(
            rt, session_id, "system", "system", content="LLM 未生成命令", session_type="chat"
        )
        return

    try:
        command = rt.executor.normalize(command_str)
        command.source = "llm"
        command.user_input = message
        command.task_id = turn_id
        command.intent = intent
        command.explanation = explanation
        command.response_mode = _normalize_response_mode(
            result.get("response_mode", ""),
            message,
            rt,
        )
        if planned_steps:
            command.step_queue = planned_steps[1:]
            command.max_steps = len(planned_steps)
        await _preview_and_apply_policy(
            websocket=websocket,
            rt=rt,
            session_id=session_id,
            command=command,
            confirm_mode=confirm_mode,
            intent=intent,
            explanation=explanation,
        )
    except ValueError as e:
        await _send_turn_state(websocket, rt, session_id, turn_id, "failed", "命令解析失败", completed=True)
        await _send(websocket, "system", content=f"命令解析失败: {e}")
        await _persist_session_message(
            rt,
            session_id,
            "system",
            "system",
            content=f"命令解析失败: {e}",
            session_type="chat",
        )


async def _try_template_skill(
    websocket: WebSocket,
    rt,
    session_id: str,
    message: str,
    confirm_mode: str,
    target: str,
    context: SessionContext,
) -> bool:
    """Try handling a chat message with a Template Skill before calling LLM."""
    if _should_skip_template_skill(message):
        return False

    default_target = target or context.current_target or _default_target_alias(rt)
    match = match_template_skill(
        message,
        server_aliases=list(getattr(rt, "servers", {}).keys()),
        default_target=default_target,
    )
    if not match:
        return False

    if match.missing_params:
        if rt.llm:
            return False
        content = (
            f"命中 Skill「{match.skill.description or match.skill.name}」，"
            f"但缺少参数: {', '.join(match.missing_params)}"
        )
        await _send(websocket, "agent", content=content)
        await _persist_session_message(
            rt, session_id, "agent", "agent", content=content, session_type="chat"
        )
        await _send_turn_state(
            websocket,
            rt,
            session_id,
            _SEND_TURN_ID.get(),
            "blocked",
            "缺少 Skill 参数",
            completed=True,
        )
        return True

    if not match.steps:
        content = f"Skill {match.skill.name} 未生成步骤"
        await _send(websocket, "system", content=content)
        await _persist_session_message(
            rt, session_id, "system", "system", content=content, session_type="chat"
        )
        await _send_turn_state(
            websocket,
            rt,
            session_id,
            _SEND_TURN_ID.get(),
            "failed",
            "Skill 未生成步骤",
            completed=True,
        )
        return True

    intro = f"命中 Skill「{match.skill.description or match.skill.name}」"
    await _send(websocket, "agent", content=intro)
    await _persist_session_message(
        rt, session_id, "agent", "agent", content=intro, session_type="chat"
    )

    first_step = match.steps[0]
    try:
        command = rt.executor.normalize(first_step["command"])
    except ValueError as e:
        content = f"Skill 命令解析失败: {e}"
        await _send(websocket, "system", content=content)
        await _persist_session_message(
            rt, session_id, "system", "system", content=content, session_type="chat"
        )
        await _send_turn_state(
            websocket,
            rt,
            session_id,
            _SEND_TURN_ID.get(),
            "failed",
            "Skill 命令解析失败",
            completed=True,
        )
        return True

    command.source = "skill"
    command.user_input = message
    command.intent = first_step.get("intent", "")
    command.explanation = first_step.get("explanation", "")
    command.response_mode = "workflow" if len(match.steps) > 1 else "raw"
    command.step_queue = match.steps[1:]
    command.max_steps = len(match.steps)
    command.skill_name = match.skill.name
    command.step_name = first_step.get("skill_step_name", "")
    _apply_skill_step_metadata(command, first_step)

    await _preview_and_apply_policy(
        websocket=websocket,
        rt=rt,
        session_id=session_id,
        command=command,
        confirm_mode=confirm_mode,
        intent=command.intent,
        explanation=command.explanation,
        channel="chat",
    )
    return True


def _is_skill_history_discovery_request(message: str) -> bool:
    text = (message or "").strip().lower()
    has_history = any(word in text for word in ("历史", "会话", "过去", "最近"))
    has_skill = "skill" in text or "技能" in text
    has_action = any(word in text for word in ("扫描", "总结", "提炼", "沉淀", "发现", "生成候选"))
    return has_history and has_skill and has_action


async def _try_skill_history_discovery(
    websocket: WebSocket,
    rt,
    session_id: str,
    message: str,
    turn_id: str,
) -> bool:
    if not _is_skill_history_discovery_request(message):
        return False
    if not getattr(rt, "db", None):
        content = "数据库未初始化，无法扫描历史任务。"
        await _send(websocket, "system", content=content)
        await _send_turn_state(websocket, rt, session_id, turn_id, "failed", "扫描失败", completed=True)
        return True
    day_match = re.search(r"(\d{1,3})\s*天", message)
    days = max(1, min(int(day_match.group(1)), 365)) if day_match else 30
    occurrence_match = re.search(r"至少\s*(\d{1,2})\s*次", message)
    min_occurrences = (
        max(2, min(int(occurrence_match.group(1)), 20))
        if occurrence_match
        else 3
    )
    semantic = "精确" not in message
    await _send_turn_state(websocket, rt, session_id, turn_id, "analyzing", "正在扫描历史任务")
    result = await discover_skill_candidates(
        rt.db,
        days=days,
        min_occurrences=min_occurrences,
        secret_values=rt.secret_values() if hasattr(rt, "secret_values") else [],
        semantic=semantic,
        llm=getattr(rt, "llm", None),
    )
    candidates = result["created"]
    existing = result["existing"]
    lines = [
        f"## Skill 候选扫描完成",
        "",
        f"- 扫描范围：最近 {days} 天",
        f"- 符合分析条件的成功任务：{result['scanned_tasks']} 个",
        f"- 重复流程组：{result['repeated_groups']} 个",
        f"- 精确分组：{result.get('exact_groups', 0)} 个",
        f"- 语义分组：{result.get('semantic_groups', 0)} 个",
        f"- 新建候选：{len(candidates)} 个",
        f"- 已存在候选：{len(existing)} 个",
    ]
    semantic_state = result.get("semantic") or {}
    if semantic:
        status = str(semantic_state.get("status") or "unavailable")
        status_text = {
            "completed": "已完成",
            "unavailable": "当前未配置可用模型，已退回精确扫描",
            "failed": "模型分组失败，已退回精确扫描",
            "insufficient_data": "可参与语义分组的任务不足",
        }.get(status, status)
        lines.append(f"- 语义扫描状态：{status_text}")
    if candidates:
        lines.extend(["", "### 新候选"])
        for index, candidate in enumerate(candidates, start=1):
            lines.extend(
                [
                    f"{index}. **{candidate['name']}**",
                    f"   - {candidate.get('description') or ''}",
                    f"   - 出现 {candidate.get('occurrence_count') or 0} 次；风险 {candidate.get('risk_level') or 'unknown'}；置信度 {float(candidate.get('confidence') or 0):.0%}",
                ]
            )
        lines.extend(["", "候选不会自动执行或启用，请到“配置 → Skills → 历史候选”审核。"])
    elif not existing:
        lines.extend(
            [
                "",
                f"没有达到“至少 {min_occurrences} 次、全部步骤成功、命令结构一致”的流程。",
                "一次性排查、失败任务、Critical 命令、已有 Skill 执行记录和无法确定性编译的语义分组不会生成候选。",
            ]
        )
    content = "\n".join(lines)
    await _send(websocket, "agent", content=content)
    await _persist_session_message(
        rt,
        session_id,
        "agent",
        "agent",
        content=content,
        payload={
            "skill_candidate_scan": {
                "days": days,
                "min_occurrences": min_occurrences,
                "candidate_ids": [item.get("id") for item in candidates],
                "semantic": semantic,
                "semantic_status": semantic_state.get("status"),
            }
        },
        session_type="chat",
    )
    await _send_turn_state(websocket, rt, session_id, turn_id, "completed", "扫描完成", completed=True)
    return True


def _should_skip_template_skill(message: str) -> bool:
    text = message or ""
    exploratory_keywords = (
        "排查", "定位", "综合", "分析", "原因", "为什么",
        "怎么回事", "是否正常", "有没有问题", "建议",
    )
    return any(keyword in text for keyword in exploratory_keywords)


def _is_artifact_deploy_request(message: str) -> bool:
    text = (message or "").lower()
    deploy_words = (
        "部署", "发版", "上线", "发布", "替换包", "更新包",
        "deploy", "release",
    )
    artifact_refs = (
        "刚上传", "上传的包", "这个包", "该包", "制品",
        "jar 包", "war 包", "tar 包", "zip 包", "jar包", "war包", "tar包", "zip包",
        "压缩包", "安装包", "包",
        ".jar", ".war", ".tar", ".tar.gz", ".tgz", ".zip",
    )
    return any(word in text for word in deploy_words) and any(
        ref in text for ref in artifact_refs
    )


def _parse_manual_memory(message: str, rt=None) -> dict | None:
    text = (message or "").strip()
    if not re.search(r"(记住|记录一下|以后记得|保存记忆|全局记忆)", text):
        return None
    body = re.sub(r"^(请)?(帮我)?(记住|记录一下|以后记得|保存记忆|全局记忆)[:,：，,\s]*", "", text).strip()
    if not body:
        return None

    target = _extract_memory_target(body, rt)
    patterns = [
        (r"(?P<subject>[\w.@+-]+|[\u4e00-\u9fff][\w.@+\-\u4e00-\u9fff]*)\s*(安装在|部署在|运行在|在)\s*(?P<value>[\w.@+/\-:]+)", "installed_on"),
        (r"(?P<subject>[\w.@+-]+|[\u4e00-\u9fff][\w.@+\-\u4e00-\u9fff]*)\s*(日志目录|日志路径)\s*(是|为|:|：)\s*(?P<value>/[^\s，。；;]+)", "log_dir"),
        (r"(?P<subject>[\w.@+-]+|[\u4e00-\u9fff][\w.@+\-\u4e00-\u9fff]*)\s*(路径|目录|位置)\s*(是|为|:|：)\s*(?P<value>/[^\s，。；;]+)", "path"),
        (r"(?P<subject>[\w.@+-]+|[\u4e00-\u9fff][\w.@+\-\u4e00-\u9fff]*)\s*(端口)\s*(是|为|:|：)\s*(?P<value>\d{2,5})", "port"),
    ]
    for pattern, predicate in patterns:
        match = re.search(pattern, body, re.I)
        if not match:
            continue
        subject = match.group("subject").strip(" ，。:：")
        value = match.group("value").strip(" ，。；;")
        if predicate == "installed_on" and rt and rt.executor:
            alias = rt.executor.resolve_server_alias(value)
            if alias:
                target = alias
                value = alias
        return {
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "target": target,
            "confidence": 1.0,
        }

    return {
        "subject": _memory_subject_from_text(body),
        "predicate": "note",
        "value": body,
        "target": target,
        "confidence": 0.8,
    }


def _extract_memory_target(text: str, rt=None) -> str:
    if not rt or not getattr(rt, "executor", None):
        return ""
    for alias in sorted(getattr(rt, "servers", {}).keys(), key=len, reverse=True):
        if alias.lower() in text.lower():
            return alias
    return ""


def _memory_subject_from_text(text: str) -> str:
    match = re.search(r"[\w.@+-]+|[\u4e00-\u9fff]{2,}", text)
    return match.group(0) if match else "note"


def _format_memory_saved(memory: dict) -> str:
    return (
        "已写入全局记忆："
        f"{memory.get('subject')} {memory.get('predicate')} {memory.get('value')}"
        + (f"（目标: {memory.get('target')}）" if memory.get("target") else "")
    )


async def _memory_history_for_input(rt, message: str, target: str = "") -> list[dict]:
    resolver = KnowledgeResolver(
        getattr(rt, "db", None),
        servers=getattr(rt, "servers", {}),
        services=getattr(rt, "services", {}),
    )
    resolution = await resolver.resolve(message, explicit_target=target)
    return resolution.as_history()


async def _send_operation_plan(
    websocket: WebSocket,
    rt,
    session_id: str,
    result: dict,
    user_input: str,
    confirm_mode: str,
) -> None:
    plan = _normalize_operation_plan(result, user_input, confirm_mode)
    if _SEND_TURN_ID.get():
        plan["turn_id"] = _SEND_TURN_ID.get()
    if not plan["recommended_approach"] and not plan["steps"]:
        content = str(result)
        await _send(websocket, "agent", content=content)
        await _persist_session_message(
            rt, session_id, "agent", "agent", content=content, session_type="chat"
        )
        if plan.get("turn_id"):
            await _send_turn_state(
                websocket,
                rt,
                session_id,
                plan["turn_id"],
                "completed",
                "已回复",
                completed=True,
            )
        return
    if plan.get("turn_id"):
        await _send_turn_state(websocket, rt, session_id, plan["turn_id"], "planning", "等待确认方案")
    rt.pending_operation_plans[_pending_plan_key(session_id)] = plan
    _get_session_context(rt, session_id).add_event(
        "操作方案", f"{plan['title']}: {plan['goal']}"
    )
    payload = _operation_plan_payload(plan, active=True)
    await _send(websocket, "operation_plan", **payload)
    await _persist_session_message(
        rt,
        session_id,
        "assistant",
        "operation_plan",
        content=plan["title"],
        payload=payload,
        session_type="chat",
    )


async def _handle_plan_adjust(
    websocket: WebSocket,
    rt,
    session_id: str,
    plan_id: str,
    instruction: str,
) -> None:
    plan = _get_pending_operation_plan(rt, session_id, plan_id)
    if not plan:
        await _send(websocket, "system", content="无待调整的方案")
        return
    instruction = (instruction or "").strip()
    if not instruction:
        await _send(websocket, "system", content="请先输入调整要求")
        return
    if not rt.llm:
        await _send(websocket, "system", content="LLM 未配置，无法调整方案")
        return

    user_adjustment = f"调整方案：{instruction}"
    await _send(websocket, "user_message", content=user_adjustment)
    await _persist_session_message(
        rt,
        session_id,
        "user",
        "user_message",
        content=user_adjustment,
        session_type="chat",
    )
    await _send(websocket, "system", content="正在调整方案...", transient=True)
    try:
        result = await rt.llm.revise_operation_plan(
            user_input=plan.get("user_input", ""),
            plan=plan.get("raw") or plan,
            adjustment=instruction,
            history=await _llm_context_history(
                rt,
                session_id,
                query=f"{plan.get('user_input', '')}\n{instruction}",
            ),
        )
    except Exception as e:
        await _send(websocket, "system", content=f"方案调整失败: {e}")
        return
    if isinstance(result, str) or not _is_operation_plan_result(result):
        await _send(websocket, "agent", content=str(result))
        return
    rt.pending_operation_plans.pop(_pending_plan_key(session_id), None)
    await _send_operation_plan(
        websocket=websocket,
        rt=rt,
        session_id=session_id,
        result=result,
        user_input=plan.get("user_input", ""),
        confirm_mode=plan.get("confirm_mode", ConfirmMode.INTERACTIVE.value),
    )


async def _handle_plan_confirm(
    websocket: WebSocket,
    rt,
    session_id: str,
    plan_id: str,
    confirmed: bool,
) -> None:
    plan = _get_pending_operation_plan(rt, session_id, plan_id)
    if not plan:
        await _send(websocket, "system", content="无待确认的方案")
        return
    rt.pending_operation_plans.pop(_pending_plan_key(session_id), None)
    if not confirmed:
        if plan.get("turn_id"):
            await _send_turn_state(websocket, rt, session_id, plan["turn_id"], "canceled", "方案已取消", completed=True)
        await _send(websocket, "system", content="已取消方案")
        await _persist_session_message(
            rt,
            session_id,
            "system",
            "system",
            content="已取消方案",
            session_type="chat",
        )
        return

    owner_key = _running_key(session_id, "plan_confirm")
    turn_id = str(plan.get("turn_id") or "")
    if turn_id:
        if not hasattr(rt, "running_task_ids"):
            rt.running_task_ids = {}
        rt.running_task_ids[owner_key] = turn_id
    turn_token = _SEND_TURN_ID.set(plan.get("turn_id", ""))
    try:
        await _send(
            websocket,
            "system",
            content="方案已确认，正在生成命令步骤...",
            transient=True,
        )
        if plan.get("turn_id"):
            await _send_turn_state(websocket, rt, session_id, plan["turn_id"], "thinking", "正在生成命令步骤")
        steps = plan.get("steps") or []
        if not steps and rt.llm:
            try:
                materialized = await rt.llm.materialize_operation_plan_steps(
                    user_input=plan.get("user_input", ""),
                    plan=plan.get("raw") or plan,
                    history=await _llm_context_history(
                        rt,
                        session_id,
                        query=plan.get("user_input", ""),
                    ),
                )
            except Exception as e:
                await _send(websocket, "system", content=f"生成命令步骤失败: {e}")
                rt.pending_operation_plans[_pending_plan_key(session_id)] = plan
                if turn_id:
                    await _send_turn_state(
                        websocket, rt, session_id, turn_id, "planning", "等待确认方案"
                    )
                return
            if isinstance(materialized, dict):
                steps = _planned_steps_from_result(materialized)
        if not steps:
            await _send(websocket, "system", content="方案中没有可执行步骤，请先调整方案")
            rt.pending_operation_plans[_pending_plan_key(session_id)] = plan
            if turn_id:
                await _send_turn_state(
                    websocket, rt, session_id, turn_id, "planning", "等待确认方案"
                )
            return
        started = await _start_planned_steps(
            websocket=websocket,
            rt=rt,
            session_id=session_id,
            user_input=plan.get("user_input", ""),
            intent=plan.get("intent") or plan.get("title") or "执行已确认方案",
            steps=steps,
            confirm_mode=plan.get("confirm_mode", ConfirmMode.INTERACTIVE.value),
            response_mode="workflow",
            turn_id=plan.get("turn_id", ""),
        )
        if not started:
            rt.pending_operation_plans[_pending_plan_key(session_id)] = plan
            if turn_id:
                await _send_turn_state(
                    websocket, rt, session_id, turn_id, "planning", "等待确认方案"
                )
    finally:
        _SEND_TURN_ID.reset(turn_token)
        if getattr(rt, "running_task_ids", {}).get(owner_key) == turn_id:
            rt.running_task_ids.pop(owner_key, None)


async def _handle_confirm(
    websocket: WebSocket,
    rt,
    session_id: str,
    confirmed: bool,
    channel: str = "chat",
    task_id: str = "",
    secondary_confirm_value: str = "",
    operation_id: str = "",
    request_id: str = "",
) -> None:
    """Handle a scoped, idempotent command confirmation request."""
    requested_operation_id = (operation_id or "").strip()
    requested_task_id = (task_id or requested_operation_id or "").strip()
    command = _get_pending_command(rt, session_id, channel, requested_task_id)
    if not command:
        command = await _restore_pending_command_from_task(
            rt, session_id, channel, requested_task_id
        )
    if not command:
        duplicate_task = await _get_scoped_task(
            rt, session_id, channel, requested_task_id
        )
        in_memory_status = _in_memory_confirmation_status(
            rt, session_id, channel, requested_task_id
        )
        if (
            duplicate_task and duplicate_task.get("status") != "waiting_confirm"
        ) or in_memory_status:
            await _send_confirm_ack(
                websocket,
                session_id=session_id,
                channel=channel,
                task_id=requested_task_id,
                operation_id=operation_id,
                request_id=request_id,
                confirmed=confirmed,
                accepted=True,
                duplicate=True,
                status=(
                    str(duplicate_task.get("status") or "accepted")
                    if duplicate_task
                    else in_memory_status
                ),
                content="确认请求已受理",
            )
            return
        await _send_confirm_ack(
            websocket,
            session_id=session_id,
            channel=channel,
            task_id=requested_task_id,
            operation_id=operation_id,
            request_id=request_id,
            confirmed=confirmed,
            accepted=False,
            duplicate=False,
            status="not_found",
            content="待确认命令不存在或不属于当前会话",
        )
        return

    requested_task_id = command.task_id or requested_task_id
    if requested_operation_id and requested_operation_id not in {
        requested_task_id,
        command.id,
    }:
        await _send_confirm_ack(
            websocket,
            session_id=session_id,
            channel=channel,
            task_id=requested_task_id,
            operation_id=requested_operation_id,
            request_id=request_id,
            confirmed=confirmed,
            accepted=False,
            duplicate=False,
            status="stale",
            content="待确认命令已变化，请确认当前步骤",
        )
        return
    if command.requires_secondary_confirm:
        expected = command.secondary_confirm_expected.strip()
        actual = (secondary_confirm_value or "").strip()
        if actual != expected:
            await _send_confirm_ack(
                websocket,
                session_id=session_id,
                channel=channel,
                task_id=requested_task_id,
                operation_id=operation_id,
                request_id=request_id,
                confirmed=confirmed,
                accepted=False,
                duplicate=False,
                status="waiting_confirm",
                content=f"二次确认不匹配，请输入 {expected} 后再执行",
            )
            await _send(
                websocket,
                "system",
                content=f"二次确认不匹配，请输入 {expected} 后再执行",
                channel=channel,
            )
            return

    claimed, claimed_status = await _claim_confirmation(
        rt, session_id, channel, requested_task_id
    )
    if not claimed:
        await _send_confirm_ack(
            websocket,
            session_id=session_id,
            channel=channel,
            task_id=requested_task_id,
            operation_id=operation_id,
            request_id=request_id,
            confirmed=confirmed,
            accepted=claimed_status != "not_found",
            duplicate=claimed_status != "not_found",
            status=claimed_status,
            content=(
                "确认请求已受理"
                if claimed_status != "not_found"
                else "待确认命令不存在或不属于当前会话"
            ),
        )
        return

    _remove_pending_command(rt, session_id, channel, requested_task_id)
    await _send_confirm_ack(
        websocket,
        session_id=session_id,
        channel=channel,
        task_id=requested_task_id,
        operation_id=operation_id,
        request_id=request_id,
        confirmed=confirmed,
        accepted=True,
        duplicate=False,
        status="confirming",
        content="确认请求已受理",
    )

    turn_token = _SEND_TURN_ID.set(command.task_id if channel == "chat" else "")
    try:
        await _handle_confirm_with_command(
            websocket,
            rt,
            session_id,
            command,
            confirmed,
            channel,
            secondary_confirm_value,
        )
    finally:
        _SEND_TURN_ID.reset(turn_token)


async def _handle_confirm_with_command(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    confirmed: bool,
    channel: str,
    secondary_confirm_value: str = "",
) -> None:
    """处理已解析出的待确认命令。调用方负责设置 turn context。"""
    if not confirmed:
        await _send(websocket, "system", content="已取消", channel=channel)
        await _send_task_step(
            websocket,
            rt,
            session_id,
            command,
            status="canceled",
            content="已取消",
            channel=channel,
        )
        await _write_audit(rt, command, executed=False, user_confirmed=False,
                           session_id=session_id)
        await _update_command_task(rt, command, status="canceled", completed=True)
        if channel == "chat":
            await _send_turn_state(websocket, rt, session_id, command.task_id, "canceled", "已取消", completed=True)
        return

    await _update_command_task(rt, command, status="running")
    if channel == "chat":
        await _send_turn_state(websocket, rt, session_id, command.task_id, "executing", "正在执行命令")
    await _send(websocket, "execution_status", status="running", content="执行中...", channel=channel)
    _start_background_execution(websocket, rt, session_id, command, channel)


async def _handle_direct_command(
    websocket: WebSocket,
    rt,
    session_id: str,
    command_str: str,
    confirm_mode: str = ConfirmMode.INTERACTIVE.value,
    target: str = "",
    cwd: str = "",
) -> None:
    """处理直接命令：支持 ssh 完整命令或基于目标的裸 shell 命令。"""
    await _ensure_session(rt, session_id, "command", title=command_str.strip()[:32])
    await _maybe_update_session_title(websocket, rt, session_id, "command", command_str)
    try:
        _apply_client_cwd(rt, session_id, target, cwd)
        is_full_ssh = command_str.strip().lower().startswith("ssh ")
        normalized_input = _normalize_direct_command_input(rt, command_str, target)
        command = rt.executor.normalize(normalized_input)
        command.source = "direct"
        if not is_full_ssh:
            _apply_direct_shell_state(rt, session_id, command)
    except ValueError as e:
        await _send(websocket, "command_error", content=f"命令解析失败: {e}", channel="command")
        return

    await _preview_and_apply_policy(
        websocket=websocket,
        rt=rt,
        session_id=session_id,
        command=command,
        confirm_mode=confirm_mode,
        channel="command",
    )


async def _handle_completion(
    websocket: WebSocket,
    rt,
    session_id: str,
    command_str: str,
    cursor: int,
    target: str = "",
    cwd: str = "",
    request_id: str = "",
    input_id: str = "",
) -> None:
    """处理直接命令输入框的 Tab 补全。"""
    _apply_client_cwd(rt, session_id, target, cwd)
    text = command_str or ""
    cursor = max(0, min(cursor, len(text)))
    token = _completion_token(text, cursor)
    candidates: list[str] = []
    kind = token["kind"]

    if text.strip().lower().startswith("ssh "):
        await _send_completion(websocket, request_id, input_id, token, kind, candidates)
        return

    if kind == "command":
        candidates = _complete_builtin_command(token["prefix"])
    else:
        candidates = await _complete_remote_path(rt, session_id, target, token["prefix"])

    await _send_completion(websocket, request_id, input_id, token, kind, candidates)


async def _send_completion(
    websocket: WebSocket,
    request_id: str,
    input_id: str,
    token: dict,
    kind: str,
    candidates: list[str],
) -> None:
    candidates = candidates[:80]
    await _send(
        websocket,
        "completion_result",
        channel="command",
        request_id=request_id,
        input_id=input_id,
        kind=kind,
        start=token["start"],
        end=token["end"],
        prefix=token["prefix"],
        candidates=candidates,
        common_prefix=_common_prefix(candidates),
    )


async def _preview_and_apply_policy(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    confirm_mode: str,
    intent: str = "",
    explanation: str = "",
    channel: str = "chat",
) -> None:
    """发送命令预览，并按 Web 端确认模式决定下一步。"""
    if channel == "chat" and not command.task_id and _SEND_TURN_ID.get():
        command.task_id = _SEND_TURN_ID.get()
    mode = _parse_confirm_mode(_effective_skill_confirm_mode(command, confirm_mode))
    command.confirm_mode = mode.value
    risk = classify_command(command.actual_command)
    policy = evaluate_environment_policy(
        env=command.target_env,
        target=command.target,
        executor=command.executor,
        risk=risk,
    )
    command.policy_blocked = policy.blocked
    command.policy_block_reason = policy.block_reason
    command.requires_secondary_confirm = policy.requires_secondary_confirm
    command.secondary_confirm_expected = policy.secondary_confirm_expected
    command.secondary_confirm_label = policy.secondary_confirm_label
    command.secondary_confirm_reason = policy.secondary_confirm_reason
    await _ensure_command_task(rt, session_id, command, channel)
    await _update_command_task(
        rt,
        command,
        status="waiting_confirm",
        current_step=command.step_index,
        total_steps=_task_total_steps(command),
        pending_command=_display_command(command),
        pending_target=command.target,
        confirm_mode=mode.value,
        workflow_snapshot=_command_workflow_snapshot(command),
    )
    _get_session_context(rt, session_id).add_generated_command(
        command,
        intent=intent,
        explanation=explanation,
    )
    await _send_task_step(
        websocket,
        rt,
        session_id,
        command,
        status="pending",
        content="等待确认",
        channel=channel,
    )
    if channel == "chat":
        await _send_turn_state(
            websocket,
            rt,
            session_id,
            command.task_id,
            "waiting_confirm",
            "等待人工确认" if _preview_needs_confirmation(mode, risk.level) else "命令已生成",
        )
    preview_payload = _command_preview_payload(rt, session_id, command, channel, risk=risk)
    await _send(websocket, "command_preview", **preview_payload)
    await _persist_session_message(
        rt,
        session_id,
        "assistant",
        "command_preview",
        content=_display_command(command),
        payload=preview_payload,
        session_type="command" if channel == "command" else "chat",
    )
    await _persist_task_event(
        rt,
        command,
        session_id,
        channel,
        "command_preview",
        status="waiting_confirm",
        step_index=command.step_index,
        content=_display_command(command),
        payload=preview_payload,
    )

    if policy.blocked:
        if channel == "chat":
            await _send_turn_state(websocket, rt, session_id, command.task_id, "blocked", "策略阻断", completed=True)
        await _send(websocket, "system", content=f"策略阻断：{policy.block_reason}", channel=channel)
        await _write_audit(
            rt,
            command,
            executed=False,
            user_confirmed=None,
            session_id=session_id,
        )
        await _update_command_task(rt, command, status="blocked", completed=True)
        return

    if mode == ConfirmMode.FULL_ACCESS:
        blocked_reason = _full_access_block_reason(command.actual_command, risk)
        if blocked_reason:
            if channel == "chat":
                await _send_turn_state(websocket, rt, session_id, command.task_id, "blocked", "命令已阻断", completed=True)
            await _send(
                websocket,
                "system",
                content=f"完全访问模式：命令命中极高风险保护，已阻断：{blocked_reason}",
                channel=channel,
            )
            await _write_audit(
                rt,
                command,
                executed=False,
                user_confirmed=None,
                session_id=session_id,
            )
            await _update_command_task(rt, command, status="blocked", completed=True)
            return
        await _send(
            websocket,
            "system",
            content="完全访问模式：命令自动执行",
            channel=channel,
            transient=True,
        )
        await _update_command_task(rt, command, status="running")
        if channel == "chat":
            await _send_turn_state(websocket, rt, session_id, command.task_id, "executing", "正在执行命令")
        await _send(websocket, "execution_status", status="running", content="执行中...", channel=channel)
        _start_background_execution(websocket, rt, session_id, command, channel)
        return

    if mode == ConfirmMode.DRY_RUN:
        await _write_audit(
            rt,
            command,
            executed=False,
            user_confirmed=None,
            session_id=session_id,
        )
        await _send(websocket, "system", content="仅预览模式：只生成命令，不执行", channel=channel)
        await _update_command_task(rt, command, status="dry_run", completed=True)
        if channel == "chat":
            await _send_turn_state(websocket, rt, session_id, command.task_id, "completed", "仅预览", completed=True)
        await _persist_session_message(
            rt,
            session_id,
            "system",
            "system",
            content="仅预览模式：只生成命令，不执行",
            payload={"channel": channel},
            session_type="command" if channel == "command" else "chat",
        )
        return

    if mode == ConfirmMode.AUTO_SAFE and risk.level == RiskLevel.SAFE:
        await _send(
            websocket,
            "system",
            content="自动安全模式：安全命令自动执行",
            channel=channel,
            transient=True,
        )
        await _update_command_task(rt, command, status="running")
        if channel == "chat":
            await _send_turn_state(websocket, rt, session_id, command.task_id, "executing", "正在执行命令")
        await _send(websocket, "execution_status", status="running", content="执行中...", channel=channel)
        _start_background_execution(websocket, rt, session_id, command, channel)
        return

    rt.pending_commands[_pending_key(session_id, channel)] = command
    if mode == ConfirmMode.AUTO_SAFE:
        await _send(
            websocket,
            "system",
            content="自动安全模式：该风险等级需要人工确认",
            channel=channel,
            transient=True,
        )
        if channel == "command":
            await _persist_session_message(
                rt,
                session_id,
                "system",
                "system",
                content="自动安全模式：该风险等级需要人工确认",
                payload={"channel": channel},
                session_type="command",
            )
    await _send(websocket, "confirm_prompt", content="确认执行? [y/n]", channel=channel)


def _pending_key(session_id: str, channel: str) -> str:
    return f"{session_id}:{channel}"


async def _get_blocking_chat_pending(rt, session_id: str) -> PendingCommand | dict | None:
    command = getattr(rt, "pending_commands", {}).get(_pending_key(session_id, "chat"))
    if command:
        return command
    return await _get_waiting_confirm_task(rt, session_id, "chat")


def _full_access_block_reason(command: str, risk) -> str:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return "空命令"
    if re.search(r"\brm\s+[^;&|]*-(?:[^\s;&|]*r[^\s;&|]*f|[^\s;&|]*f[^\s;&|]*r)\b[^;&|]*(\s|=)(/|/[*.]|--no-preserve-root)(\s|$|[;&|])", normalized, re.I):
        return "递归强制删除根目录或根目录通配路径"
    if re.search(r"\b(mkfs|mke2fs|wipefs)\b|\bdd\b[^;&|]*\bof=/dev/", normalized, re.I):
        return "格式化或裸块设备写入属于灾难性操作"
    if "rm_recursive" in getattr(risk, "rules", []) and re.search(r"\s/(?:\s|$|[*.;|&])", normalized):
        return "递归强制删除系统根路径"
    return ""


async def _get_waiting_confirm_task(rt, session_id: str, channel: str) -> dict | None:
    if not getattr(rt, "db", None):
        return None
    tasks = await get_session_tasks(rt.db, session_id, channel=channel, include_completed=False)
    for task in tasks:
        if task.get("status") == "waiting_confirm" and task.get("pending_command"):
            return task
    return None


async def _get_scoped_task(
    rt,
    session_id: str,
    channel: str,
    task_id: str,
) -> dict | None:
    if not task_id or not getattr(rt, "db", None):
        return None
    task = await get_task(rt.db, task_id)
    if not task:
        return None
    if task.get("session_id") != session_id or task.get("channel") != channel:
        return None
    return task


def _confirmation_claim_key(session_id: str, channel: str, task_id: str) -> str:
    return f"{session_id}:{channel}:{task_id or '_legacy'}"


def _in_memory_confirmation_status(
    rt,
    session_id: str,
    channel: str,
    task_id: str,
) -> str:
    claims = getattr(rt, "confirmation_claims", {})
    return str(claims.get(_confirmation_claim_key(session_id, channel, task_id)) or "")


async def _claim_confirmation(
    rt,
    session_id: str,
    channel: str,
    task_id: str,
) -> tuple[bool, str]:
    if task_id and getattr(rt, "db", None):
        claimed, task = await claim_task_confirmation(
            rt.db, task_id, session_id, channel
        )
        if claimed:
            return True, "confirming"
        return False, str(task.get("status") or "accepted") if task else "not_found"

    if not hasattr(rt, "confirmation_claims"):
        rt.confirmation_claims = {}
    key = _confirmation_claim_key(session_id, channel, task_id)
    status = rt.confirmation_claims.get(key)
    if status:
        return False, str(status)
    rt.confirmation_claims[key] = "confirming"
    return True, "confirming"


async def _send_confirm_ack(
    websocket: WebSocket,
    *,
    session_id: str,
    channel: str,
    task_id: str,
    operation_id: str,
    request_id: str,
    confirmed: bool,
    accepted: bool,
    duplicate: bool,
    status: str,
    content: str,
) -> None:
    await _send(
        websocket,
        "confirm_ack",
        session_id=session_id,
        channel=channel,
        task_id=task_id,
        operation_id=operation_id or task_id,
        request_id=request_id,
        confirmed=confirmed,
        accepted=accepted,
        duplicate=duplicate,
        status=status,
        content=content,
    )


def _format_pending_command_block_message(pending: PendingCommand | dict) -> str:
    if isinstance(pending, PendingCommand):
        command = _display_command(pending)
        target = pending.target
    else:
        command = str(pending.get("pending_command") or "")
        target = str(pending.get("pending_target") or "")
    return (
        "当前会话还有待确认命令，请先执行或取消后再继续。\n"
        f"待确认命令：{command}\n"
        f"目标：{target}"
    )


def _running_key(session_id: str, channel: str) -> str:
    return f"{session_id}:{channel}"


def _chat_turn_key(session_id: str) -> str:
    return _running_key(session_id, "chat_turn")


def _get_pending_command(
    rt,
    session_id: str,
    channel: str,
    task_id: str = "",
) -> PendingCommand | None:
    command = getattr(rt, "pending_commands", {}).get(
        _pending_key(session_id, channel)
    )
    if not command:
        return None
    if task_id and task_id not in {command.task_id, command.id}:
        return None
    return command


def _remove_pending_command(
    rt,
    session_id: str,
    channel: str,
    task_id: str = "",
) -> PendingCommand | None:
    key = _pending_key(session_id, channel)
    command = getattr(rt, "pending_commands", {}).get(key)
    if not command or (task_id and command.task_id and command.task_id != task_id):
        return None
    return rt.pending_commands.pop(key, None)


def _command_preview_payload(
    rt,
    session_id: str,
    command: PendingCommand,
    channel: str,
    risk=None,
) -> dict:
    risk = risk or classify_command(command.actual_command)
    context = _get_session_context(rt, session_id)
    return {
        "session_id": session_id,
        "task_id": command.task_id,
        "operation_id": command.id,
        "turn_id": command.task_id if channel == "chat" else "",
        "channel": channel,
        "step_index": command.step_index,
        "total_steps": _task_total_steps(command),
        "command": _display_command(command),
        "target": command.target,
        "cwd": context.get_cwd(command.target),
        "intent": command.intent,
        "explanation": command.explanation,
        "skill_name": command.skill_name,
        "skill_version": command.skill_version,
        "skill_hash": command.skill_hash,
        "skill_step_name": command.step_name,
        "confirm_mode": command.confirm_mode,
        "policy_blocked": command.policy_blocked,
        "policy_block_reason": command.policy_block_reason,
        "requires_secondary_confirm": command.requires_secondary_confirm,
        "secondary_confirm_expected": command.secondary_confirm_expected,
        "secondary_confirm_label": command.secondary_confirm_label,
        "secondary_confirm_reason": command.secondary_confirm_reason,
        **risk.as_payload(),
    }


async def _ensure_command_task(
    rt,
    session_id: str,
    command: PendingCommand,
    channel: str,
) -> None:
    if command.task_id or not getattr(rt, "db", None):
        return
    task = await create_task(
        rt.db,
        session_id=session_id,
        channel=channel,
        title=command.user_input or command.intent or _display_command(command),
        total_steps=_task_total_steps(command),
        confirm_mode=command.confirm_mode,
    )
    command.task_id = task["id"]


async def _update_command_task(rt, command: PendingCommand, **kwargs) -> None:
    if not command.task_id or not getattr(rt, "db", None):
        return
    await update_task(rt.db, command.task_id, **kwargs)


async def _ensure_session(rt, session_id: str, session_type: str, title: str = "") -> None:
    if getattr(rt, "db", None) and session_id:
        await ensure_session(rt.db, session_id, session_type=session_type, title=title)


async def _maybe_update_session_title(
    websocket: WebSocket,
    rt,
    session_id: str,
    session_type: str,
    user_input: str,
) -> None:
    if not getattr(rt, "db", None) or not session_id:
        return
    title = await maybe_update_title_from_user_message(rt.db, session_id, user_input)
    if title:
        await _send(
            websocket,
            "session_updated",
            title=title,
            session_type=session_type,
            channel="command" if session_type == "command" else "chat",
        )


async def _persist_session_message(
    rt,
    session_id: str,
    role: str,
    msg_type: str,
    content: str = "",
    payload: dict | None = None,
    session_type: str = "chat",
) -> None:
    if not getattr(rt, "db", None) or not session_id:
        return
    payload = dict(payload or {})
    turn_id = payload.get("turn_id") or _SEND_TURN_ID.get()
    if turn_id:
        payload["turn_id"] = turn_id
    await ensure_session(rt.db, session_id, session_type=session_type)
    await add_session_message(
        rt.db,
        session_id=session_id,
        role=role,
        msg_type=msg_type,
        content=content,
        payload=payload,
    )


async def _persist_task_event(
    rt,
    command: PendingCommand,
    session_id: str,
    channel: str,
    event_type: str,
    *,
    status: str = "",
    step_index: int | None = None,
    content: str = "",
    payload: dict | None = None,
) -> None:
    if not command.task_id or not getattr(rt, "db", None):
        return
    payload = dict(payload or {})
    turn_id = payload.get("turn_id") or (_SEND_TURN_ID.get() if channel == "chat" else "")
    if turn_id:
        payload["turn_id"] = turn_id
    await add_task_event(
        rt.db,
        command.task_id,
        session_id,
        channel,
        event_type,
        status=status,
        step_index=step_index,
        content=content,
        payload=payload,
    )


def _start_background_execution(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    channel: str,
) -> None:
    key = _running_key(session_id, channel)
    existing = getattr(rt, "running_tasks", {}).get(key)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_run_execution_flow(websocket, rt, session_id, command, channel))
    if not hasattr(rt, "running_tasks"):
        rt.running_tasks = {}
    if not hasattr(rt, "running_task_ids"):
        rt.running_task_ids = {}
    rt.running_tasks[key] = task
    if command.task_id:
        rt.running_task_ids[key] = command.task_id


def _start_chat_turn(
    websocket: WebSocket,
    rt,
    session_id: str,
    message: str,
    confirm_mode: str,
    target: str,
) -> None:
    if not message.strip():
        return
    if not hasattr(rt, "running_tasks"):
        rt.running_tasks = {}
    key = _chat_turn_key(session_id)
    existing = rt.running_tasks.get(key)
    if existing and not existing.done():
        asyncio.create_task(
            _send(
                websocket,
                "system",
                content="Opsane 正在回复中，请先停止当前任务后再发送新消息",
                channel="chat",
                session_id=session_id,
            )
        )
        return
    task = asyncio.create_task(
        _run_chat_turn(websocket, rt, session_id, message, confirm_mode, target)
    )
    rt.running_tasks[key] = task


async def _run_chat_turn(
    websocket: WebSocket,
    rt,
    session_id: str,
    message: str,
    confirm_mode: str,
    target: str,
) -> None:
    key = _chat_turn_key(session_id)
    token = _SEND_SESSION_ID.set(session_id)
    try:
        await _handle_chat_message(websocket, rt, session_id, message, confirm_mode, target)
    except asyncio.CancelledError:
        # `_handle_chat_message` owns the persisted turn id and writes the
        # cancellation state before the task unwinds.
        pass
    except Exception as exc:
        logger.exception(f"聊天协程异常: session={session_id} error={exc}")
    finally:
        _SEND_SESSION_ID.reset(token)
        if hasattr(rt, "running_tasks") and rt.running_tasks.get(key) is asyncio.current_task():
            rt.running_tasks.pop(key, None)


def _schedule_task_learning(
    rt,
    *,
    task_id: str,
    session_id: str,
    user_input: str,
    final_summary: str = "",
) -> None:
    """Run post-task learning without extending or mutating the user turn state."""
    if (
        not task_id
        or not getattr(rt, "db", None)
        or not getattr(rt, "llm", None)
        or task_id in getattr(rt, "learning_task_ids", set())
    ):
        return
    rt.learning_task_ids.add(task_id)

    async def run() -> None:
        try:
            result = await learn_from_task(
                rt.db,
                rt.llm,
                task_id=task_id,
                session_id=session_id,
                user_input=user_input,
                final_summary=final_summary,
                services=getattr(rt, "services", {}),
                servers=getattr(rt, "servers", {}),
                secret_values=rt.secret_values() if hasattr(rt, "secret_values") else [],
            )
            if result.errors:
                logger.warning(
                    f"任务知识学习未完成: task={task_id} errors={'; '.join(result.errors)}"
                )
            elif result.memory_count or result.candidate_count:
                logger.info(
                    f"任务知识学习完成: task={task_id} memories={result.memory_count} "
                    f"candidates={result.candidate_count}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"任务知识学习异常: task={task_id} error={exc}")

    task = asyncio.create_task(run())
    rt.background_tasks.add(task)
    task.add_done_callback(rt.background_tasks.discard)


async def _run_execution_flow(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    channel: str,
) -> None:
    key = _running_key(session_id, channel)
    turn_token = _SEND_TURN_ID.set(command.task_id if channel == "chat" else "")
    try:
        await _send_task_step(
            websocket,
            rt,
            session_id,
            command,
            status="running",
            content="执行中",
            channel=channel,
        )
        result = await _execute(rt, command, session_id)
        _update_direct_cwd_after_execution(rt, session_id, command, result)
        _record_execution_context(rt, session_id, command, result, channel=channel)
        await _send_execution_status(websocket, result, channel=channel)
        await _send_execution_result(websocket, rt, session_id, command, result, channel=channel)
        chat_lifecycle = channel == "chat"
        if result.timed_out:
            task_status = "failed"
        elif _result_success(result):
            task_status = "success"
        elif _result_partial_success(result):
            task_status = "partial"
        else:
            task_status = "failed"
        await _update_command_task(
            rt,
            command,
            status="running" if chat_lifecycle else task_status,
            completed=not chat_lifecycle,
        )
        await _send_task_step(
            websocket,
            rt,
            session_id,
            command,
            status=_task_step_status_from_result(result),
            content=_task_step_content_from_result(result),
            channel=channel,
        )
        lifecycle_handled = await _maybe_analyze_execution_result(
            websocket, rt, session_id, command, result, channel
        )
        if channel == "chat" and not lifecycle_handled:
            if result.timed_out:
                await _send_turn_state(websocket, rt, session_id, command.task_id, "timeout", "执行超时", completed=True)
            elif _result_success(result) or _result_partial_success(result):
                await _send_turn_state(websocket, rt, session_id, command.task_id, "completed", "任务完成", completed=True)
            else:
                await _send_turn_state(websocket, rt, session_id, command.task_id, "failed", "执行失败", completed=True)
            if _result_success(result) or _result_partial_success(result):
                _schedule_task_learning(
                    rt,
                    task_id=command.task_id,
                    session_id=session_id,
                    user_input=command.user_input or command.intent,
                )
    except asyncio.CancelledError:
        result = ExecutionResult(
            exit_code=None,
            stdout="",
            stderr="执行已取消",
            duration_ms=0,
        )
        await _write_audit(
            rt,
            command,
            executed=True,
            user_confirmed=True,
            session_id=session_id,
            exit_code=None,
            duration_ms=0,
            stdout="",
            stderr=result.stderr,
        )
        await _send(websocket, "execution_status", status="canceled", content="执行已取消", channel=channel)
        await _update_command_task(rt, command, status="canceled", completed=True)
        await _send_task_step(
            websocket,
            rt,
            session_id,
            command,
            status="canceled",
            content="已取消",
            channel=channel,
        )
        await _send_execution_result(websocket, rt, session_id, command, result, channel=channel)
        if channel == "chat":
            await _send_turn_state(websocket, rt, session_id, command.task_id, "canceled", "已取消", completed=True)
    except Exception as exc:
        logger.exception(
            f"命令执行流程异常: task={command.task_id} target={command.target} error={exc}"
        )
        await _update_command_task(rt, command, status="failed", completed=True)
        await _send(websocket, "system", content=f"任务执行异常: {exc}", channel=channel)
        if channel == "chat":
            await _terminalize_chat_turn(
                websocket,
                rt,
                session_id,
                command.task_id,
                "failed",
                "任务执行异常",
            )
    finally:
        _SEND_TURN_ID.reset(turn_token)
        if hasattr(rt, "running_tasks") and rt.running_tasks.get(key) is asyncio.current_task():
            rt.running_tasks.pop(key, None)
        if getattr(rt, "running_task_ids", {}).get(key) == command.task_id:
            rt.running_task_ids.pop(key, None)


async def _handle_cancel(
    websocket: WebSocket,
    rt,
    session_id: str,
    channel: str = "command",
) -> None:
    pending = rt.pending_commands.pop(_pending_key(session_id, channel), None)
    if pending:
        turn_token = _SEND_TURN_ID.set(pending.task_id if channel == "chat" else "")
        await _write_audit(
            rt,
            pending,
            executed=False,
            user_confirmed=False,
            session_id=session_id,
        )
        await _update_command_task(rt, pending, status="canceled", completed=True)
        await _send(websocket, "execution_status", status="canceled", content="已取消", channel=channel)
        await _send_task_step(
            websocket,
            rt,
            session_id,
            pending,
            status="canceled",
            content="已取消",
            channel=channel,
        )
        await _send(websocket, "system", content="已取消", channel=channel)
        await _persist_session_message(
            rt,
            session_id,
            "system",
            "system",
            content="已取消",
            payload={"channel": channel},
            session_type="command" if channel == "command" else "chat",
        )
        if channel == "chat":
            await _send_turn_state(websocket, rt, session_id, pending.task_id, "canceled", "已取消", completed=True)
        _SEND_TURN_ID.reset(turn_token)
        return

    if channel == "chat":
        plan = rt.pending_operation_plans.pop(_pending_plan_key(session_id), None)
        if plan:
            turn_id = str(plan.get("turn_id") or "")
            await _terminalize_chat_turn(
                websocket,
                rt,
                session_id,
                turn_id,
                "canceled",
                "方案已取消",
            )
            await _send(websocket, "system", content="已取消方案", channel="chat")
            await _persist_session_message(
                rt,
                session_id,
                "system",
                "system",
                content="已取消方案",
                payload={"channel": "chat", "turn_id": turn_id},
                session_type="chat",
            )
            return

    cancel_keys = [_running_key(session_id, channel)]
    if channel == "chat":
        cancel_keys.insert(0, _chat_turn_key(session_id))
    canceled_running = False
    for key in cancel_keys:
        task = getattr(rt, "running_tasks", {}).get(key)
        if task and not task.done():
            task.cancel()
            canceled_running = True
    if canceled_running:
        await _send(websocket, "execution_status", status="stopping", content="正在停止", channel=channel)
        return

    await _send(websocket, "system", content="无正在执行的命令", channel=channel)
    await _persist_session_message(
        rt,
        session_id,
        "system",
        "system",
        content="无正在执行的命令",
        payload={"channel": channel},
        session_type="command" if channel == "command" else "chat",
    )


def _record_execution_context(
    rt,
    session_id: str,
    command: PendingCommand,
    result: ExecutionResult,
    channel: str = "chat",
) -> None:
    _get_session_context(rt, session_id).add_execution_result(
        command,
        result,
        channel=channel,
    )


def _strip_embedded_command_payload(content: str) -> str:
    """Remove command JSON emitted outside the structured command protocol."""
    fenced_json = re.compile(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        re.IGNORECASE | re.DOTALL,
    )

    def replace(match: re.Match[str]) -> str:
        payload = match.group(1)
        if re.search(r'"(?:command|next_command|steps)"\s*:', payload):
            return ""
        return match.group(0)

    return fenced_json.sub(replace, content or "").strip()


async def _maybe_analyze_execution_result(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    result: ExecutionResult,
    channel: str,
) -> bool:
    """Handle post-execution chat work.

    Returns True when this function owns the remaining task lifecycle by either
    scheduling a next step or writing a terminal task state.
    """
    if channel != "chat":
        return False

    output_summary = f"执行结果摘要:\n{compact_text(_result_output(result), limit=2000)}"
    if command.source == "skill":
        if not _result_success(result) and command.skill_on_failure != "continue":
            await _fail_chat_task(
                websocket,
                rt,
                session_id,
                command,
                f"Skill 步骤「{command.step_name or command.step_index}」执行失败，已停止后续步骤",
            )
            return True
        if not _result_success(result):
            command.skill_had_failures = True
        if command.step_queue:
            await _maybe_plan_next_step(
                websocket=websocket,
                rt=rt,
                session_id=session_id,
                command=command,
                analysis=output_summary,
                confirm_mode=_parse_confirm_mode_value(command),
            )
            return True
        if _is_task_command(command):
            content = (
                "Skill 任务部分完成：至少一个步骤失败"
                if command.skill_had_failures
                else "Skill 任务已完成"
            )
            await _send_task_complete(websocket, rt, session_id, command, content)
            return True
        return False

    if command.source != "llm":
        return False
    if not rt.llm:
        if _is_task_command(command):
            await _send_task_complete(websocket, rt, session_id, command, "任务已完成")
            return True
        return False
    if command.response_mode in ("collect", "workflow"):
        await _maybe_plan_next_step(
            websocket=websocket,
            rt=rt,
            session_id=session_id,
            command=command,
            analysis=output_summary,
            confirm_mode=_parse_confirm_mode_value(command),
        )
        return True
    if command.response_mode == "raw":
        await _maybe_summarize_single_chat_result(websocket, rt, session_id, command, result)
        return False
    if command.response_mode not in ("analyze", "investigate"):
        return False

    await _send_turn_state(
        websocket,
        rt,
        session_id,
        command.task_id,
        "analyzing",
        "正在分析结果",
    )
    await _send(
        websocket,
        "system",
        content="正在分析结果...",
        channel=channel,
        transient=True,
    )
    try:
        analysis = await rt.llm.analyze_execution_result(
            user_input=command.user_input or command.intent,
            command=command.actual_command,
            output=compact_text(_result_output(result), limit=2000),
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            history=await _llm_context_history(
                rt,
                session_id,
                query=command.user_input or command.intent,
            ),
        )
    except Exception as e:
        await _send(websocket, "system", content=f"结果分析失败: {e}", channel=channel)
        if command.response_mode == "investigate":
            await _fail_chat_task(
                websocket,
                rt,
                session_id,
                command,
                "结果分析失败，任务已停止",
            )
            return True
        return False

    if analysis:
        visible_analysis = _strip_embedded_command_payload(analysis)
        if visible_analysis:
            _get_session_context(rt, session_id).add_event("结果分析", visible_analysis)
            await _send(websocket, "agent", content=visible_analysis)
            await _persist_session_message(
                rt,
                session_id,
                "agent",
                "agent",
                content=visible_analysis,
                session_type="chat",
            )
        await _maybe_plan_next_step(
            websocket=websocket,
            rt=rt,
            session_id=session_id,
            command=command,
            analysis=visible_analysis or "当前结果不足以完成用户目标，需要继续检查。",
            confirm_mode=_parse_confirm_mode_value(command),
        )
        return command.response_mode == "investigate"
    if command.response_mode == "investigate":
        await _send_task_complete(websocket, rt, session_id, command, "任务已完成")
        return True
    return False


async def _maybe_summarize_single_chat_result(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    result: ExecutionResult,
) -> None:
    """对事实判断型的单条聊天命令追加简短结论，避免把直接命令页变成聊天分析器。"""
    if not _should_summarize_single_chat_result(command, result):
        return
    if not hasattr(rt.llm, "summarize_task_result"):
        await _send(websocket, "execution_status", status="success", content="执行完成", channel="chat")
        return

    await _send_turn_state(
        websocket,
        rt,
        session_id,
        command.task_id,
        "analyzing",
        "正在生成结论",
    )
    await _send(
        websocket,
        "system",
        content="正在生成最终结论...",
        channel="chat",
        transient=True,
    )
    task_outputs = "\n".join(
        [
            "Step 1",
            f"- target: {command.target}",
            f"- command: {_display_command(command)}",
            f"- status: {'success' if _result_success(result) else 'partial' if _result_partial_success(result) else 'failed'}",
            f"- exit_code: {result.exit_code}",
            f"- timed_out: {result.timed_out}",
            "- output:",
            compact_text(_result_output(result), limit=2200),
        ]
    )
    try:
        summary = await rt.llm.summarize_task_result(
            user_input=command.user_input or command.intent,
            task_outputs=task_outputs,
            draft_summary="",
            history=await _llm_context_history(
                rt,
                session_id,
                query=command.user_input or command.intent,
            ),
        )
    except Exception as e:
        logger.warning(f"单条命令最终结论生成失败: {e}")
        await _send(websocket, "execution_status", status="success", content="执行完成", channel="chat")
        return

    summary = (summary or "").strip()
    if not summary or _is_weak_final_summary(summary):
        await _send(websocket, "execution_status", status="success", content="执行完成", channel="chat")
        return
    _get_session_context(rt, session_id).add_event("最终结论", summary)
    await _send(websocket, "agent", content=summary)
    await _persist_session_message(
        rt,
        session_id,
        "agent",
        "agent",
        content=summary,
        session_type="chat",
    )
    await _send(websocket, "execution_status", status="success", content="执行完成", channel="chat")


def _should_summarize_single_chat_result(command: PendingCommand, result: ExecutionResult) -> bool:
    if command.source != "llm" or command.response_mode != "raw":
        return False
    if result.timed_out or not (_result_success(result) or _result_partial_success(result)):
        return False
    text = f"{command.user_input or ''} {command.intent or ''}".lower()
    fact_keywords = (
        "是否",
        "有没有",
        "几个",
        "多少",
        "哪个",
        "哪台",
        "状态",
        "正常",
        "安装",
        "版本",
        "时间",
        "创建",
        "修改",
        "属性",
        "占用",
        "大小",
        "空间",
        "资源",
        "进程",
        "服务",
        "端口",
        "路径",
        "能不能",
        "可用",
        "不能用",
        "对比",
        "区别",
    )
    command_text = command.actual_command.lower()
    fact_commands = (
        "stat ",
        "which ",
        "java -version",
        "javac -version",
        "nginx -v",
        "systemctl ",
        "rpm -qa",
        "dpkg -l",
        "df ",
        "du ",
        "free ",
        "uptime",
        "ps ",
        "ss ",
        "netstat ",
    )
    return any(keyword in text for keyword in fact_keywords) or any(
        marker in command_text for marker in fact_commands
    )


def _parse_confirm_mode_value(command: PendingCommand) -> str:
    return getattr(command, "confirm_mode", ConfirmMode.INTERACTIVE.value)


async def _maybe_plan_next_step(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    analysis: str,
    confirm_mode: str,
) -> None:
    """让 LLM 决定聊天任务是否需要继续下一条命令。"""
    if command.response_mode not in ("collect", "workflow", "investigate"):
        return

    if command.step_queue:
        next_step = command.step_queue[0]
        try:
            pending = rt.executor.normalize(next_step["command"])
            pending.source = "skill" if command.source == "skill" else "llm"
            pending.user_input = command.user_input
            pending.intent = next_step.get("intent", "")
            pending.explanation = next_step.get("explanation", "")
            pending.response_mode = command.response_mode
            pending.step_index = command.step_index + 1
            pending.max_steps = command.max_steps
            pending.step_queue = command.step_queue[1:]
            pending.confirm_mode = confirm_mode
            pending.task_id = command.task_id
            pending.skill_name = command.skill_name
            pending.step_name = next_step.get("skill_step_name", "")
            if pending.source == "skill":
                pending.skill_version = command.skill_version
                pending.skill_hash = command.skill_hash
                pending.skill_default_confirm_mode = command.skill_default_confirm_mode
                pending.skill_had_failures = command.skill_had_failures
                _apply_skill_step_metadata(pending, next_step)
        except ValueError as e:
            await _fail_chat_task(
                websocket,
                rt,
                session_id,
                command,
                f"下一步命令解析失败: {e}",
            )
            return

        await _send(websocket, "agent", content=pending.intent or "继续执行下一步")
        await _preview_and_apply_policy(
            websocket=websocket,
            rt=rt,
            session_id=session_id,
            command=pending,
            confirm_mode=confirm_mode,
            intent=pending.intent,
            explanation=pending.explanation,
            channel="chat",
        )
        return

    if not rt.llm:
        await _send_task_complete(
            websocket,
            rt,
            session_id,
            command,
            "没有可继续执行的模板步骤，任务已停止",
        )
        return

    await _send_turn_state(
        websocket,
        rt,
        session_id,
        command.task_id,
        "analyzing",
        "正在判断下一步",
    )
    await _send(
        websocket,
        "system",
        content="正在判断是否需要继续下一步...",
        channel="chat",
        transient=True,
    )
    try:
        decision = await rt.llm.decide_next_step(
            user_input=command.user_input or command.intent,
            command=command.actual_command,
            analysis=analysis,
            step_index=command.step_index,
            max_steps=command.max_steps,
            history=await _llm_context_history(
                rt,
                session_id,
                query=command.user_input or command.intent,
            ),
        )
    except Exception as e:
        await _fail_chat_task(
            websocket,
            rt,
            session_id,
            command,
            f"下一步决策失败: {e}",
        )
        return

    if not isinstance(decision, dict):
        await _send_task_complete(
            websocket,
            rt,
            session_id,
            command,
            str(decision) or "任务已完成",
        )
        return

    summary = decision.get("summary", "")
    if summary and command.response_mode not in ("collect", "workflow"):
        await _send(websocket, "agent", content=summary)

    if decision.get("done", True):
        await _send_task_complete(
            websocket,
            rt,
            session_id,
            command,
            summary or "任务已完成",
        )
        return

    next_command = decision.get("next_command", "")
    if not next_command:
        await _fail_chat_task(
            websocket,
            rt,
            session_id,
            command,
            "下一步决策未返回命令，任务已停止",
        )
        return

    try:
        pending = rt.executor.normalize(next_command)
        pending.source = "llm"
        pending.user_input = command.user_input
        pending.intent = decision.get("next_intent", "")
        pending.explanation = decision.get("next_explanation", "")
        pending.response_mode = command.response_mode
        pending.step_index = command.step_index + 1
        pending.max_steps = command.max_steps
        pending.confirm_mode = confirm_mode
        pending.task_id = command.task_id
    except ValueError as e:
        await _fail_chat_task(
            websocket,
            rt,
            session_id,
            command,
            f"下一步命令解析失败: {e}",
        )
        return

    await _send(websocket, "agent", content=pending.intent or "需要继续执行下一步")
    await _preview_and_apply_policy(
        websocket=websocket,
        rt=rt,
        session_id=session_id,
        command=pending,
        confirm_mode=confirm_mode,
        intent=pending.intent,
        explanation=pending.explanation,
        channel="chat",
    )


def _is_task_command(command: PendingCommand) -> bool:
    return (
        command.response_mode in ("collect", "workflow", "investigate")
        or bool(command.step_queue)
    )


def _task_total_steps(command: PendingCommand) -> int:
    if command.max_steps > 0:
        return max(command.max_steps, command.step_index)
    planned_total = command.step_index + len(command.step_queue)
    if command.step_queue:
        return max(planned_total, command.step_index)
    return 0


def _task_step_status_from_result(result: ExecutionResult) -> str:
    if result.timed_out:
        return "timeout"
    if _result_success(result):
        return "success"
    if _result_partial_success(result):
        return "partial"
    return "failed"


def _task_step_content_from_result(result: ExecutionResult) -> str:
    if result.timed_out:
        return "执行超时"
    if _result_success(result):
        return "执行完成"
    if _result_partial_success(result):
        return "已返回结果（退出码非 0）"
    return "执行失败"


async def _send_task_step(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    status: str,
    content: str,
    channel: str = "chat",
) -> None:
    if channel != "chat" or not _is_task_command(command):
        return
    payload = {
        "task_id": command.task_id,
        "turn_id": command.task_id,
        "channel": channel,
        "step_index": command.step_index,
        "total_steps": _task_total_steps(command),
        "status": status,
        "content": content,
        "intent": command.intent or command.step_name or "",
        "command": _display_command(command),
        "target": command.target,
    }
    parent_status = "waiting_confirm" if status == "pending" else status
    if status in {"running", "success", "partial", "failed", "timeout"}:
        parent_status = "running"
    await _update_command_task(
        rt,
        command,
        status=parent_status,
        current_step=command.step_index,
        total_steps=_task_total_steps(command),
        pending_command=_display_command(command) if status == "pending" else "",
        pending_target=command.target if status == "pending" else "",
    )
    await _send(websocket, "task_step", **payload)
    await _persist_task_event(
        rt,
        command,
        session_id,
        channel,
        "task_step",
        status=status,
        step_index=command.step_index,
        content=content,
        payload=payload,
    )
    await _persist_session_message(
        rt,
        session_id,
        "assistant",
        "task_step",
        content=content,
        payload=payload,
        session_type="chat",
    )


async def _send_task_complete(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    content: str,
) -> None:
    if not _is_task_command(command):
        return
    await _send_turn_state(
        websocket,
        rt,
        session_id,
        command.task_id,
        "analyzing",
        "正在生成最终结论",
    )
    await _send(
        websocket,
        "system",
        content="正在生成最终结论...",
        channel="chat",
        transient=True,
    )
    final_content = await _build_final_task_conclusion(
        rt,
        session_id,
        command,
        content,
    )
    if command.skill_had_failures:
        warning = "⚠️ Skill 任务部分完成：至少一个步骤失败。"
        if warning not in final_content:
            final_content = f"{warning}\n\n{final_content}".strip()
    payload = {
        "task_id": command.task_id,
        "turn_id": command.task_id,
        "channel": "chat",
        "step_index": command.step_index,
        "total_steps": _task_total_steps(command),
        "status": "complete",
        "content": final_content,
        "intent": "任务完成",
        "command": "",
        "target": command.target,
    }
    terminal_status = "partial" if command.skill_had_failures else "success"
    await _update_command_task(rt, command, status=terminal_status, completed=True)
    await _send(websocket, "task_step", **payload)
    await _persist_task_event(
        rt,
        command,
        session_id,
        "chat",
        "task_step",
        status="complete",
        step_index=command.step_index,
        content=final_content,
        payload=payload,
    )
    await _persist_session_message(
        rt,
        session_id,
        "assistant",
        "task_step",
        content=final_content,
        payload=payload,
        session_type="chat",
    )
    turn_label = "任务部分完成" if command.skill_had_failures else "任务完成"
    await _send_turn_state(
        websocket, rt, session_id, command.task_id, "completed", turn_label, completed=True
    )
    if command.skill_had_failures:
        await _update_command_task(rt, command, status="partial", completed=True)
    else:
        _schedule_task_learning(
            rt,
            task_id=command.task_id,
            session_id=session_id,
            user_input=command.user_input or command.intent,
            final_summary=final_content,
        )


async def _fail_chat_task(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    content: str,
) -> None:
    """Close a workflow that cannot make or execute its next decision."""
    await _send(websocket, "system", content=content, channel="chat")
    await _persist_session_message(
        rt,
        session_id,
        "system",
        "system",
        content=content,
        payload={"channel": "chat", "turn_id": command.task_id},
        session_type="chat",
    )
    await _update_command_task(rt, command, status="failed", completed=True)
    await _terminalize_chat_turn(
        websocket,
        rt,
        session_id,
        command.task_id,
        "failed",
        "任务已停止",
    )


async def _build_final_task_conclusion(
    rt,
    session_id: str,
    command: PendingCommand,
    draft_summary: str,
) -> str:
    if not getattr(rt, "llm", None):
        return draft_summary
    task_outputs = await _task_outputs_for_summary(rt, command.task_id)
    if not task_outputs:
        return draft_summary
    try:
        summary = await rt.llm.summarize_task_result(
            user_input=command.user_input or command.intent,
            task_outputs=task_outputs,
            draft_summary=draft_summary,
            history=await _llm_context_history(
                rt,
                session_id,
                query=command.user_input or command.intent,
            ),
        )
    except Exception as e:
        logger.warning(f"最终结论汇总失败: {e}")
        return draft_summary
    summary = (summary or "").strip()
    if not summary:
        return draft_summary
    if _is_weak_final_summary(summary) and not _is_weak_final_summary(draft_summary):
        return draft_summary
    _get_session_context(rt, session_id).add_event("最终结论", summary)
    return summary


async def _task_outputs_for_summary(rt, task_id: str) -> str:
    if not task_id or not getattr(rt, "db", None):
        return ""
    events = await get_task_events(rt.db, task_id)
    chunks: list[str] = []
    for event in events:
        if event.get("type") != "execution_result":
            continue
        payload = event.get("payload") or {}
        command = payload.get("command") or ""
        target = payload.get("target") or ""
        exit_code = payload.get("exit_code")
        timed_out = payload.get("timed_out")
        status = event.get("status") or ""
        if payload.get("partial_success"):
            status = "partial"
        output = payload.get("output") or event.get("content") or ""
        chunks.append(
            "\n".join(
                [
                    f"Step {event.get('step_index') or '?'}",
                    f"- target: {target}",
                    f"- command: {command}",
                    f"- status: {status}",
                    f"- exit_code: {exit_code}",
                    f"- timed_out: {timed_out}",
                    "- output:",
                    compact_text(output, limit=2200),
                ]
            )
        )
    return compact_text("\n\n".join(chunks), limit=6000)


def _is_weak_final_summary(summary: str) -> bool:
    text = (summary or "").strip()
    if not text:
        return True
    weak_phrases = (
        "可以总结",
        "可以进行总结",
        "已经获取到",
        "已获取到",
        "任务已完成",
        "任务已停止",
    )
    return len(text) < 12 or any(phrase in text and len(text) < 80 for phrase in weak_phrases)


def _normalize_response_mode(value: str, user_input: str, rt=None) -> str:
    mode = (value or "").strip().lower()
    mentioned_count = _mentioned_server_count(rt, user_input)

    text = user_input or ""
    investigate_keywords = (
        "排查",
        "定位",
        "诊断",
        "综合",
        "一步步",
        "继续查",
        "查清楚",
        "查原因",
        "找原因",
        "什么原因",
        "具体原因",
        "失败原因",
        "根因",
        "为什么",
    )
    workflow_keywords = (
        "日志内容", "看日志", "查看日志", "看报错", "最近日志",
        "最新日志", "错误日志", "异常日志",
    )
    analyze_keywords = (
        "总结", "分析", "判断", "异常", "怎么",
        "风险", "建议", "是否正常", "有没有问题", "说明下",
    )
    if any(keyword in text for keyword in investigate_keywords):
        return "investigate"
    if mentioned_count > 1:
        return "collect"
    if any(keyword in text for keyword in workflow_keywords):
        return "workflow"
    if mode in {"raw", "workflow", "collect", "analyze", "investigate"}:
        return mode
    if any(keyword in text for keyword in analyze_keywords):
        return "analyze"
    return "raw"


async def _start_planned_steps(
    websocket: WebSocket,
    rt,
    session_id: str,
    user_input: str,
    intent: str,
    steps: list[dict[str, str]],
    confirm_mode: str,
    response_mode: str = "workflow",
    turn_id: str = "",
) -> bool:
    first_step = steps[0] if steps else {}
    command_str = first_step.get("command", "")
    if not command_str:
        await _send(websocket, "system", content="方案步骤缺少命令")
        return False
    try:
        command = rt.executor.normalize(command_str)
    except ValueError as e:
        await _send(websocket, "system", content=f"方案命令解析失败: {e}")
        return False

    command.source = "llm"
    command.user_input = user_input
    command.intent = first_step.get("intent") or intent
    command.explanation = first_step.get("explanation", "")
    command.response_mode = response_mode
    command.task_id = turn_id or _SEND_TURN_ID.get()
    command.step_queue = steps[1:]
    command.max_steps = len(steps)
    await _send(websocket, "agent", content=intent or command.intent or "开始执行已确认方案")
    await _persist_session_message(
        rt,
        session_id,
        "agent",
        "agent",
        content=intent or command.intent or "开始执行已确认方案",
        session_type="chat",
    )
    await _preview_and_apply_policy(
        websocket=websocket,
        rt=rt,
        session_id=session_id,
        command=command,
        confirm_mode=confirm_mode,
        intent=command.intent,
        explanation=command.explanation,
        channel="chat",
    )
    return True


def _mentioned_server_count(rt, user_input: str) -> int:
    if not rt or not getattr(rt, "servers", None) or not user_input:
        return 0
    text = user_input.lower()
    count = 0
    for alias in rt.servers:
        if alias.lower() in text:
            count += 1
    return count


def _parse_confirm_mode(confirm_mode: str) -> ConfirmMode:
    try:
        return ConfirmMode(confirm_mode)
    except ValueError:
        return ConfirmMode.INTERACTIVE


_CONFIRM_MODE_RANK = {
    ConfirmMode.FULL_ACCESS.value: 0,
    ConfirmMode.AUTO_SAFE.value: 1,
    ConfirmMode.INTERACTIVE.value: 2,
}


def _effective_skill_confirm_mode(command: PendingCommand, requested: str) -> str:
    """Skill 只能收紧用户选择的确认模式，不能放宽。"""
    requested_mode = _parse_confirm_mode(requested)
    if command.source != "skill" and not command.skill_name:
        return requested_mode.value
    if requested_mode == ConfirmMode.DRY_RUN:
        return requested_mode.value
    if command.skill_force_confirm:
        return ConfirmMode.INTERACTIVE.value
    default_mode = _parse_confirm_mode(command.skill_default_confirm_mode)
    if default_mode == ConfirmMode.DRY_RUN:
        return default_mode.value
    requested_rank = _CONFIRM_MODE_RANK.get(requested_mode.value, 2)
    default_rank = _CONFIRM_MODE_RANK.get(default_mode.value, 2)
    return requested_mode.value if requested_rank >= default_rank else default_mode.value


def _apply_skill_step_metadata(command: PendingCommand, step: dict) -> None:
    command.skill_name = str(step.get("skill_name") or command.skill_name or "")
    command.skill_version = str(step.get("skill_version") or command.skill_version or "")
    command.skill_hash = str(step.get("skill_hash") or command.skill_hash or "")
    command.skill_default_confirm_mode = str(
        step.get("skill_default_confirm_mode")
        or command.skill_default_confirm_mode
        or ConfirmMode.INTERACTIVE.value
    )
    command.skill_force_confirm = bool(step.get("confirm", True))
    failure_policy = str(step.get("on_failure") or "abort")
    command.skill_on_failure = failure_policy if failure_policy in {"abort", "continue"} else "abort"
    timeout = step.get("timeout_seconds")
    command.timeout_seconds = int(timeout) if timeout is not None else None


def _command_workflow_snapshot(command: PendingCommand) -> dict:
    return {
        "operation_id": command.id,
        "source": command.source,
        "user_input": command.user_input,
        "intent": command.intent,
        "explanation": command.explanation,
        "response_mode": command.response_mode,
        "step_index": command.step_index,
        "max_steps": command.max_steps,
        "step_queue": command.step_queue,
        "skill_name": command.skill_name,
        "skill_version": command.skill_version,
        "skill_hash": command.skill_hash,
        "skill_default_confirm_mode": command.skill_default_confirm_mode,
        "skill_force_confirm": command.skill_force_confirm,
        "skill_on_failure": command.skill_on_failure,
        "skill_had_failures": command.skill_had_failures,
        "step_name": command.step_name,
        "timeout_seconds": command.timeout_seconds,
        "policy_blocked": command.policy_blocked,
        "policy_block_reason": command.policy_block_reason,
        "requires_secondary_confirm": command.requires_secondary_confirm,
        "secondary_confirm_expected": command.secondary_confirm_expected,
        "secondary_confirm_label": command.secondary_confirm_label,
        "secondary_confirm_reason": command.secondary_confirm_reason,
    }


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Accept same-origin browsers and non-browser local clients only."""
    headers = getattr(websocket, "headers", None)
    if headers is None:
        return True
    origin = str(headers.get("origin") or "").strip()
    if not origin:
        return True
    host = str(headers.get("host") or "").strip().lower()
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.netloc.lower() == host:
        return True
    origin_host = (parsed.hostname or "").lower()
    try:
        request_host = (urlsplit(f"//{host}").hostname or "").lower()
    except ValueError:
        return False
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    return origin_host in loopback_hosts and request_host in loopback_hosts


def _preview_needs_confirmation(mode: ConfirmMode, risk_level: RiskLevel) -> bool:
    if mode in (ConfirmMode.DRY_RUN, ConfirmMode.FULL_ACCESS):
        return False
    if mode == ConfirmMode.AUTO_SAFE:
        return risk_level != RiskLevel.SAFE
    return True


async def _execute(rt, command: PendingCommand, session_id: str) -> ExecutionResult:
    """执行命令"""
    try:
        if command.timeout_seconds is not None and hasattr(rt.executor, "default_timeout"):
            result = await rt.executor.execute(command, timeout=command.timeout_seconds)
        else:
            result = await rt.executor.execute(command)
    except Exception as e:
        logger.exception("执行器异常")
        result = ExecutionResult(
            exit_code=None,
            stdout="",
            stderr=f"执行器异常: {e}",
            duration_ms=0,
        )
        await _write_audit(
            rt, command, executed=True, user_confirmed=True,
            session_id=session_id, exit_code=None, duration_ms=0,
            stdout="", stderr=result.stderr,
        )
        return result

    await _write_audit(
        rt, command, executed=True, user_confirmed=True,
        session_id=session_id, exit_code=result.exit_code,
        duration_ms=result.duration_ms, stdout=result.stdout,
        stderr=result.stderr, truncated=result.truncated,
        timed_out=result.timed_out,
    )
    return result


def _result_success(result: ExecutionResult) -> bool:
    return result.exit_code == 0 and not result.timed_out


def _result_partial_success(result: ExecutionResult) -> bool:
    return (
        not result.timed_out
        and result.exit_code not in (None, 0)
        and bool((result.stdout or "").strip())
    )


def _result_output(result: ExecutionResult) -> str:
    output = result.stdout
    if result.stderr:
        output = f"{output}\n[stderr]\n{result.stderr}" if output else result.stderr
    return output


async def _send_execution_status(
    websocket: WebSocket,
    result: ExecutionResult,
    channel: str = "chat",
) -> None:
    if result.timed_out:
        content = "执行超时"
        status = "timeout"
    elif _result_success(result):
        content = "执行完成"
        status = "success"
    elif _result_partial_success(result):
        content = "已返回结果（退出码非 0）"
        status = "partial"
    else:
        content = "执行失败"
        status = "failed"
    await _send(websocket, "execution_status", status=status, content=content, channel=channel)


async def _send_execution_result(
    websocket: WebSocket,
    rt,
    session_id: str,
    command: PendingCommand,
    result: ExecutionResult,
    channel: str = "chat",
) -> None:
    success = _result_success(result)
    partial_success = _result_partial_success(result)
    output = _result_output(result)
    payload = {
        "channel": channel,
        "task_id": command.task_id,
        "turn_id": command.task_id if channel == "chat" else "",
        "success": success,
        "partial_success": partial_success,
        "output": output,
        "exit_code": result.exit_code if result.exit_code is not None else 1,
        "timed_out": result.timed_out,
        "command": _display_command(command),
        "target": command.target,
        "cwd": _get_session_context(rt, session_id).get_cwd(command.target),
    }
    await _send(
        websocket,
        "execution_result",
        **payload,
    )
    await _persist_session_message(
        rt,
        session_id,
        "assistant",
        "execution_result",
        content=output,
        payload=payload,
        session_type="command" if channel == "command" else "chat",
    )
    await _persist_task_event(
        rt,
        command,
        session_id,
        channel,
        "execution_result",
        status="success" if success else "partial" if partial_success else "failed",
        step_index=command.step_index,
        content=output,
        payload=payload,
    )


async def _write_audit(
    rt, command: PendingCommand, executed: bool,
    user_confirmed: bool | None = None, session_id: str = "",
    exit_code: int | None = None, duration_ms: int | None = None,
    stdout: str | None = None, stderr: str | None = None,
    truncated: bool = False, timed_out: bool = False,
) -> None:
    record = AuditRecord(
        command=command.actual_command,
        target=command.target,
        target_env=command.target_env,
        executor=command.executor,
        executed=executed,
        source=command.source,
        caller="web_user",
        session_id=session_id,
        user_confirmed=user_confirmed,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout=stdout, stderr=stderr,
        truncated=truncated, timed_out=timed_out,
    )
    try:
        await write_audit(rt.db, record)
    except Exception as e:
        logger.error(f"审计写入失败: {e}")
