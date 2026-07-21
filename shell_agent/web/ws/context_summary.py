"""Incremental, persisted semantic summaries for chat session context."""
from __future__ import annotations

import asyncio

from loguru import logger

from shell_agent.core.context import SessionContext, compact_text
from shell_agent.core.redaction import redact_context_secrets
from shell_agent.attachments.context import build_attachment_history
from shell_agent.knowledge.redaction import SecretRedactor
from shell_agent.storage.sessions import get_session, update_session_context_summary
from shell_agent.utils.config import ContextConfig
from shell_agent.web.ws.session_state import (
    _apply_persisted_message_to_context,
    _get_hydrated_session_context,
)


def _context_config(rt) -> ContextConfig:
    configured = getattr(getattr(rt, "config", None), "context", None)
    return configured if configured is not None else ContextConfig()


def _runtime_redact(rt, text: str) -> str:
    value = redact_context_secrets(text)
    secret_values = rt.secret_values() if hasattr(rt, "secret_values") else []
    return SecretRedactor(secret_values).redact_text(value)


def _semantic_entry(message: dict) -> str:
    payload = message.get("payload") or {}
    message_type = message.get("type")
    timestamp = str(message.get("created_at") or "")
    prefix = f"[{timestamp}] " if timestamp else ""
    if message_type == "user_message":
        body = f"用户请求: {message.get('content') or ''}"
    elif message_type == "command_preview":
        body = (
            f"生成命令: target={payload.get('target') or ''}; "
            f"command={payload.get('command') or message.get('content') or ''}; "
            f"intent={payload.get('intent') or ''}"
        )
    elif message_type == "execution_result":
        body = (
            f"执行结果: target={payload.get('target') or ''}; "
            f"command={payload.get('command') or ''}; "
            f"success={bool(payload.get('success'))}; exit_code={payload.get('exit_code')}; "
            f"output={compact_text(str(payload.get('output') or message.get('content') or ''), limit=1200)}"
        )
    elif message_type == "agent":
        body = f"Agent回复: {compact_text(str(message.get('content') or ''), limit=800)}"
    elif message_type == "operation_plan":
        body = (
            f"操作方案: {payload.get('title') or message.get('content') or ''}; "
            f"goal={payload.get('goal') or ''}"
        )
    elif message_type == "task_step" and payload.get("status") == "complete":
        body = f"最终结论: {compact_text(str(message.get('content') or payload.get('content') or ''), limit=1200)}"
    elif message_type == "artifact_upload":
        artifact = payload.get("artifact") or {}
        status = str(artifact.get("status") or "").lower()
        if status in {"failed", "error", "interrupted", "cancelled", "canceled"}:
            body = (
                f"文件传输失败: target={artifact.get('target') or ''}; "
                f"filename={artifact.get('filename') or ''}; remote_path={artifact.get('remote_path') or ''}; "
                f"error={compact_text(str(artifact.get('error') or message.get('content') or ''), limit=600)}"
            )
        else:
            body = (
                f"上传制品: target={artifact.get('target') or ''}; "
                f"filename={artifact.get('filename') or ''}; remote_path={artifact.get('remote_path') or ''}; "
                f"size={artifact.get('size') or ''}; sha256={artifact.get('sha256') or ''}"
            )
    else:
        return ""
    return redact_context_secrets(prefix + body).strip()


async def _maybe_refresh_semantic_summary(rt, session_id: str) -> SessionContext:
    context = await _get_hydrated_session_context(rt, session_id)
    config = _context_config(rt)
    if (
        not config.semantic_summary_enabled
        or not getattr(rt, "db", None)
        or not getattr(rt, "llm", None)
        or not hasattr(rt.llm, "summarize_context")
    ):
        return context

    if not hasattr(rt, "context_summary_locks"):
        rt.context_summary_locks = {}
    lock = rt.context_summary_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        session = await get_session(rt.db, session_id)
        if not session:
            return context
        messages = session.get("messages", [])
        covered_count = min(
            max(0, int(session.get("context_summary_message_count") or 0)),
            len(messages),
        )
        pending_messages = messages[covered_count:]
        relevant: list[tuple[int, str]] = []
        for index, message in enumerate(pending_messages):
            entry = _semantic_entry(message)
            if entry:
                relevant.append((index, entry))

        total_chars = sum(len(entry) for _, entry in relevant)
        trigger_events = max(4, int(config.summary_trigger_events))
        trigger_chars = max(2000, int(config.summary_trigger_chars))
        if len(relevant) <= trigger_events and total_chars <= trigger_chars:
            return context

        recent_events = max(1, int(config.recent_events))
        retain_count = min(recent_events, max(1, len(relevant) - 1))
        candidates = relevant[:-retain_count]
        if not candidates:
            return context
        cutoff_relative = candidates[-1][0] + 1
        cutoff_absolute = covered_count + cutoff_relative
        source_text = _runtime_redact(rt, "\n".join(entry for _, entry in candidates))
        previous_summary = _runtime_redact(rt, str(session.get("context_summary") or ""))

        try:
            summary = await asyncio.wait_for(
                rt.llm.summarize_context(
                    previous_summary=previous_summary,
                    events=source_text,
                    max_tokens=max(128, int(config.summary_max_tokens)),
                ),
                timeout=max(0.1, float(config.summary_timeout_seconds)),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "会话语义摘要超时，继续使用规则压缩: "
                f"session={session_id} timeout={config.summary_timeout_seconds}s"
            )
            return context
        except Exception as exc:
            logger.warning(f"会话语义摘要失败，继续使用规则压缩: {exc}")
            return context

        summary = _runtime_redact(rt, str(summary or "")).strip()
        if len(summary) < 20:
            logger.warning("会话语义摘要过短，继续使用规则压缩")
            return context
        summary = compact_text(summary, limit=max(800, int(config.summary_max_chars)))
        await update_session_context_summary(
            rt.db,
            session_id,
            summary,
            cutoff_absolute,
        )

        context.rolling_summary = summary
        context.semantic_summary = True
        context.events.clear()
        for message in messages[cutoff_absolute:]:
            _apply_persisted_message_to_context(context, message)
        setattr(context, "_summary_message_count", cutoff_absolute)
        logger.info(
            f"会话语义摘要已更新: session={session_id} "
            f"covered={cutoff_absolute}/{len(messages)} recent={len(context.events)}"
        )
        return context


async def _llm_context_history(rt, session_id: str, query: str = "") -> list[dict]:
    context = await _maybe_refresh_semantic_summary(rt, session_id)
    session_history = [
        {**message, "content": _runtime_redact(rt, str(message.get("content") or ""))}
        for message in context.to_llm_history()
    ]
    attachment_history = await build_attachment_history(
        getattr(rt, "db", None),
        session_id,
        query,
        redact=lambda value: _runtime_redact(rt, value),
    )
    return [*attachment_history, *session_history]
