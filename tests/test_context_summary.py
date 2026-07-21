import asyncio
from types import SimpleNamespace

import pytest

from shell_agent.core.context import SessionContext
from shell_agent.core.redaction import redact_context_secrets
from shell_agent.storage.database import connect, init_db
from shell_agent.storage.sessions import add_session_message, ensure_session, get_session
from shell_agent.utils.config import ContextConfig
from shell_agent.web.ws.context_summary import _llm_context_history


class SummaryLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def summarize_context(self, previous_summary: str, events: str, max_tokens: int) -> str:
        self.calls.append(
            {
                "previous_summary": previous_summary,
                "events": events,
                "max_tokens": max_tokens,
            }
        )
        return "## 已完成与关键事实\n- dev-01 已完成检查\n- token=summary-secret"


class SlowSummaryLLM(SummaryLLM):
    async def summarize_context(self, previous_summary: str, events: str, max_tokens: int) -> str:
        self.calls.append(
            {
                "previous_summary": previous_summary,
                "events": events,
                "max_tokens": max_tokens,
            }
        )
        await asyncio.sleep(10)
        return "不会返回的摘要"


def _runtime(db, llm) -> SimpleNamespace:
    return SimpleNamespace(
        db=db,
        llm=llm,
        config=SimpleNamespace(
            context=ContextConfig(
                summary_trigger_events=4,
                summary_trigger_chars=100000,
                recent_events=2,
                summary_max_chars=1200,
                summary_max_tokens=333,
            )
        ),
        session_contexts={},
        context_summary_locks={},
        secret_values=lambda: ["runtime-only-secret"],
    )


async def _add_dialogue(db, session_id: str, start: int, count: int) -> None:
    for index in range(start, start + count):
        if index % 2 == 0:
            content = f"检查 dev-01 第 {index} 项 password=user-secret runtime-only-secret"
            await add_session_message(db, session_id, "user", "user_message", content)
        else:
            await add_session_message(
                db,
                session_id,
                "assistant",
                "agent",
                f"第 {index} 项已完成",
            )


def test_context_secret_redaction_covers_common_credentials() -> None:
    text = " ".join(
        [
            "password=hunter2",
            "Authorization: Bearer abcdefghijklmnop",
            "api_key=sk-1234567890abcdefgh",
            "mysql://root:db-secret@db/app",
            "--token cli-secret",
            "-----BEGIN PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----",
        ]
    )

    redacted = redact_context_secrets(text)

    for secret in ["hunter2", "abcdefghijklmnop", "sk-1234567890abcdefgh", "db-secret", "cli-secret", "private-body"]:
        assert secret not in redacted
    assert "[REDACTED" in redacted


def test_recent_context_is_redacted_before_semantic_summary_is_needed() -> None:
    context = SessionContext("sess_redaction")
    context.add_event("用户请求", "使用 password=plain-secret 连接")

    history = context.to_llm_history()

    assert "plain-secret" not in history[0]["content"]
    assert "password=[REDACTED]" in history[0]["content"]


@pytest.mark.asyncio
async def test_semantic_summary_is_incremental_persisted_and_rehydrated(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        session_id = "sess_semantic"
        await ensure_session(db, session_id, session_type="chat", title="语义摘要")
        await _add_dialogue(db, session_id, 0, 6)
        llm = SummaryLLM()
        runtime = _runtime(db, llm)

        history = await _llm_context_history(runtime, session_id)

        assert len(llm.calls) == 1
        assert llm.calls[0]["max_tokens"] == 333
        assert "user-secret" not in llm.calls[0]["events"]
        assert "runtime-only-secret" not in llm.calls[0]["events"]
        assert "password=[REDACTED]" in llm.calls[0]["events"]
        assert "summary-secret" not in history[0]["content"]
        assert "较早会话语义摘要" in history[0]["content"]
        persisted = await get_session(db, session_id)
        assert persisted["context_summary_message_count"] == 4
        assert "summary-secret" not in persisted["context_summary"]

        await _llm_context_history(runtime, session_id)
        assert len(llm.calls) == 1

        restarted_runtime = _runtime(db, llm)
        restarted_history = await _llm_context_history(restarted_runtime, session_id)
        assert len(llm.calls) == 1
        assert "较早会话语义摘要" in restarted_history[0]["content"]
        assert "第 4 项" in restarted_history[0]["content"]
        assert "第 5 项" in restarted_history[0]["content"]

        await _add_dialogue(db, session_id, 6, 4)
        await _llm_context_history(restarted_runtime, session_id)
        assert len(llm.calls) == 2
        assert "dev-01 已完成检查" in llm.calls[1]["previous_summary"]
        persisted = await get_session(db, session_id)
        assert persisted["context_summary_message_count"] == 8
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_semantic_summary_timeout_falls_back_to_existing_context(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        session_id = "sess_semantic_timeout"
        await ensure_session(db, session_id, session_type="chat", title="摘要超时")
        await _add_dialogue(db, session_id, 0, 6)
        llm = SlowSummaryLLM()
        runtime = _runtime(db, llm)
        runtime.config.context.summary_timeout_seconds = 0.1

        history = await asyncio.wait_for(
            _llm_context_history(runtime, session_id),
            timeout=1,
        )

        assert len(llm.calls) == 1
        assert history
        persisted = await get_session(db, session_id)
        assert persisted["context_summary_message_count"] == 0
        assert not persisted["context_summary"]
    finally:
        await db.close()
