"""WebSocket session context and direct-shell state helpers."""
from __future__ import annotations

import re
import shlex

from shell_agent.core.context import SessionContext, compact_text
from shell_agent.core.models import ExecutionResult, PendingCommand
from shell_agent.storage.sessions import get_session


_FAILED_ARTIFACT_STATUSES = {"failed", "error", "interrupted", "cancelled", "canceled"}


def _artifact_failed(artifact: dict) -> bool:
    return str(artifact.get("status") or "").lower() in _FAILED_ARTIFACT_STATUSES


def _get_session_context(rt, session_id: str) -> SessionContext:
    """Return the in-memory context for a WebSocket session."""
    if not hasattr(rt, "session_contexts"):
        rt.session_contexts = {}
    if session_id not in rt.session_contexts:
        rt.session_contexts[session_id] = SessionContext(session_id=session_id)
    return rt.session_contexts[session_id]


async def _get_hydrated_session_context(rt, session_id: str) -> SessionContext:
    """Hydrate a session context from persisted messages at most once."""
    context = _get_session_context(rt, session_id)
    if getattr(context, "_hydrated", False):
        return context
    if not getattr(rt, "db", None):
        setattr(context, "_hydrated", True)
        return context
    session = await get_session(rt.db, session_id)
    if not session:
        setattr(context, "_hydrated", True)
        return context
    messages = session.get("messages", [])
    covered_count = min(
        max(0, int(session.get("context_summary_message_count") or 0)),
        len(messages),
    )
    persisted_summary = str(session.get("context_summary") or "").strip()
    if persisted_summary:
        context.rolling_summary = persisted_summary
        context.semantic_summary = True

    # Structured state remains useful even when its source messages are covered by the summary.
    for message in messages:
        payload = message.get("payload") or {}
        if message.get("type") in {"command_preview", "execution_result"}:
            context.set_target(str(payload.get("target") or ""))
        elif message.get("type") == "artifact_upload":
            artifact = payload.get("artifact") or {}
            if artifact and not _artifact_failed(artifact):
                context.add_artifact_upload(artifact, record_event=False)

    for message in messages[covered_count:]:
        _apply_persisted_message_to_context(context, message)
    setattr(context, "_summary_message_count", covered_count)
    setattr(context, "_hydrated", True)
    return context


def _apply_persisted_message_to_context(context: SessionContext, message: dict) -> None:
    """Replay one persisted message into the recent in-memory context."""
    payload = message.get("payload") or {}
    timestamp = str(message.get("created_at") or "")
    message_type = message.get("type")
    if message_type == "user_message":
        context.add_event("用户请求", message.get("content") or "", timestamp=timestamp)
    elif message_type == "command_preview":
        target = str(payload.get("target") or "")
        command = payload.get("command", message.get("content") or "")
        context.set_target(target)
        context.add_event("生成命令", f"{target} $ {command}", timestamp=timestamp)
    elif message_type == "execution_result":
        target = str(payload.get("target") or "")
        command = payload.get("command", "")
        output = payload.get("output", message.get("content") or "")
        context.set_target(target)
        context.add_event(
            "执行结果",
            f"{target} $ {command} -> "
            f"{'成功' if payload.get('success') else '失败'}\n"
            f"输出摘要:\n{compact_text(output)}",
            timestamp=timestamp,
        )
    elif message_type == "agent":
        context.add_event(
            "Agent回复",
            compact_text(message.get("content") or "", limit=500),
            timestamp=timestamp,
        )
    elif message_type == "operation_plan":
        context.add_event(
            "操作方案",
            compact_text(message.get("content") or payload.get("title") or "", limit=800),
            timestamp=timestamp,
        )
    elif message_type == "task_step" and payload.get("status") == "complete":
        context.add_event(
            "最终结论",
            compact_text(message.get("content") or payload.get("content") or "", limit=1000),
            timestamp=timestamp,
        )
    elif message_type == "artifact_upload":
        artifact = payload.get("artifact") or {}
        if artifact and _artifact_failed(artifact):
            context.add_event(
                "文件传输失败",
                compact_text(
                    str(artifact.get("error") or message.get("content") or "文件传输失败"),
                    limit=500,
                ),
                timestamp=timestamp,
            )
        elif artifact:
            context.add_artifact_upload(artifact)


def _apply_client_cwd(rt, session_id: str, target: str, cwd: str) -> None:
    """Apply a valid client-provided working directory to session state."""
    if not target or not cwd:
        return
    if not (cwd.startswith("/") or cwd.startswith("~")):
        return
    _get_session_context(rt, session_id).set_cwd(target, cwd)


def _display_command(command: PendingCommand) -> str:
    return command.display_command or command.actual_command


def _apply_direct_shell_state(rt, session_id: str, command: PendingCommand) -> None:
    """为直接命令模拟每台服务器的当前工作目录。"""
    context = _get_session_context(rt, session_id)
    command.display_command = command.actual_command

    cd_target = _parse_simple_cd(command.actual_command)
    if cd_target is not None:
        prefix = _cwd_prefix(context.get_cwd(command.target), cd_target)
        cd_fragment = "cd" if cd_target == "" else f"cd {_quote_cd_target(cd_target)}"
        command.actual_command = f"{prefix}{cd_fragment} && pwd"
        command.cwd_update = True
        return

    cwd = context.get_cwd(command.target)
    if cwd:
        command.actual_command = f"cd {shlex.quote(cwd)} && {command.actual_command}"


def _parse_simple_cd(command: str) -> str | None:
    text = command.strip()
    if not text.startswith("cd") or not re.match(r"^cd(\s|$)", text):
        return None
    if re.search(r"[;&|<>]", text):
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        return None
    if len(parts) == 1:
        return ""
    if len(parts) == 2:
        return parts[1]
    return None


def _cwd_prefix(current_cwd: str, cd_target: str) -> str:
    if not current_cwd:
        return ""
    if cd_target == "" or cd_target.startswith("/") or cd_target.startswith("~"):
        return ""
    return f"cd {shlex.quote(current_cwd)} && "


def _quote_cd_target(path: str) -> str:
    if path in ("", "~"):
        return ""
    if path.startswith("~") and not re.search(r"\s", path):
        return path
    return shlex.quote(path)


def _update_direct_cwd_after_execution(
    rt,
    session_id: str,
    command: PendingCommand,
    result: ExecutionResult,
) -> None:
    if (
        command.source != "direct"
        or not command.cwd_update
        or result.exit_code != 0
        or result.timed_out
    ):
        return
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if lines:
        _get_session_context(rt, session_id).set_cwd(command.target, lines[-1])


def _normalize_direct_command_input(rt, command_str: str, target: str = "") -> str:
    text = command_str.strip()
    if not text:
        raise ValueError("命令不能为空")
    if text.lower().startswith("ssh "):
        return text

    target_alias = target or _default_target_alias(rt)
    if not target_alias:
        raise ValueError("未选择目标服务器，请先在服务器页选择或配置服务器")
    return f"ssh {target_alias} {text!r}"


def _default_target_alias(rt) -> str:
    if not rt.servers:
        return ""
    return next(iter(rt.servers.keys()))
