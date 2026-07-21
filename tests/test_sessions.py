import pytest

from shell_agent.storage.database import connect, init_db
from shell_agent.storage.sessions import (
    add_session_message,
    create_session,
    ensure_session,
    get_session,
    maybe_update_title_from_user_message,
    optimize_session_title,
    update_session_context_summary,
)


def test_optimize_session_title_removes_weak_prefixes() -> None:
    assert optimize_session_title("帮我查询一下 dev-01 磁盘空间") == "dev-01 磁盘空间"


def test_optimize_session_title_compacts_ssh_command() -> None:
    assert optimize_session_title("ssh dev-01 'df -h'") == "dev-01 · df -h"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_type", "placeholder"),
    [
        ("chat", "新聊天"),
        ("chat", "新会话"),
        ("command", "新命令会话"),
        ("command", "命令终端"),
    ],
)
async def test_first_input_replaces_new_session_placeholder(
    tmp_path,
    session_type: str,
    placeholder: str,
) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        session = await create_session(db, session_type=session_type, title=placeholder)

        updated_title = await maybe_update_title_from_user_message(
            db,
            session["id"],
            "帮我查询一下 dev-01 磁盘空间",
        )
        saved = await get_session(db, session["id"])

        assert updated_title == "dev-01 磁盘空间"
        assert saved["title"] == "dev-01 磁盘空间"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_first_input_does_not_replace_custom_session_title(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        session = await create_session(db, session_type="chat", title="生产巡检")

        updated_title = await maybe_update_title_from_user_message(db, session["id"], "检查磁盘")
        saved = await get_session(db, session["id"])

        assert updated_title is None
        assert saved["title"] == "生产巡检"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_session_messages_preserve_insert_order_with_same_timestamp(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await ensure_session(db, "sess_order", session_type="chat", title="顺序测试")
        await add_session_message(db, "sess_order", "user", "user_message", "问题一")
        await add_session_message(db, "sess_order", "assistant", "agent", "回答一")
        await add_session_message(db, "sess_order", "user", "user_message", "问题二")

        await db.execute("UPDATE session_messages SET created_at = ?", ("2026-07-09T10:00:00",))
        await db.commit()

        session = await get_session(db, "sess_order")

        assert [message["content"] for message in session["messages"]] == ["问题一", "回答一", "问题二"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_session_context_summary_persists_without_reordering_session(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await ensure_session(db, "sess_summary", session_type="chat", title="摘要测试")
        await add_session_message(db, "sess_summary", "user", "user_message", "检查服务")
        before = await get_session(db, "sess_summary")

        await update_session_context_summary(db, "sess_summary", "## 关键事实\n- 正常", 1)
        after = await get_session(db, "sess_summary")

        assert after["context_summary"] == "## 关键事实\n- 正常"
        assert after["context_summary_message_count"] == 1
        assert after["context_summary_updated_at"]
        assert after["updated_at"] == before["updated_at"]
    finally:
        await db.close()
