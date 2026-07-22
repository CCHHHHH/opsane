import pytest

from shell_agent.storage.database import connect, init_db
from shell_agent.storage.tasks import (
    add_task_event,
    claim_task_confirmation,
    create_task,
    get_session_tasks,
    get_task,
    get_task_events,
    update_task,
)


@pytest.mark.asyncio
async def test_task_confirmation_claim_is_scoped_and_atomic(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await db.execute(
            """
            INSERT INTO sessions (id, title, type, caller, source, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("sess_claim", "确认测试", "chat", "test", "web", "active", "now", "now"),
        )
        await db.commit()
        task = await create_task(db, "sess_claim", "chat", title="检查状态")
        await update_task(
            db,
            task["id"],
            status="waiting_confirm",
            pending_command="uptime",
            pending_target="dev-01",
        )

        wrong_scope, wrong_task = await claim_task_confirmation(
            db, task["id"], "sess_other", "chat"
        )
        first, claimed_task = await claim_task_confirmation(
            db, task["id"], "sess_claim", "chat"
        )
        second, duplicate_task = await claim_task_confirmation(
            db, task["id"], "sess_claim", "chat"
        )

        assert wrong_scope is False
        assert wrong_task is None
        assert first is True
        assert claimed_task["status"] == "confirming"
        assert second is False
        assert duplicate_task["status"] == "confirming"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_task_persists_frozen_skill_workflow_snapshot(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await db.execute(
            """
            INSERT INTO sessions (id, title, type, caller, source, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("sess_skill", "Skill", "chat", "test", "web", "active", "now", "now"),
        )
        await db.commit()
        task = await create_task(db, "sess_skill", "chat", title="资源检查")
        snapshot = {
            "source": "skill",
            "skill_name": "resource_summary",
            "skill_version": "2",
            "skill_hash": "abc123",
            "step_queue": [{"command": "ssh dev-01 uptime"}],
        }
        await update_task(
            db,
            task["id"],
            status="waiting_confirm",
            pending_command="df -h",
            pending_target="dev-01",
            workflow_snapshot=snapshot,
        )

        stored = await get_task(db, task["id"])
        assert stored is not None
        assert stored["workflow_snapshot"] == snapshot
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_task_storage_tracks_status_and_events(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await db.execute(
            """
            INSERT INTO sessions (id, title, type, caller, source, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("sess_task", "任务测试", "chat", "test", "web", "active", "now", "now"),
        )
        await db.commit()

        task = await create_task(
            db,
            session_id="sess_task",
            channel="chat",
            title="查看日志",
            total_steps=2,
            confirm_mode="auto_safe",
        )
        assert task["status"] == "pending"
        assert task["total_steps"] == 2

        await update_task(
            db,
            task["id"],
            status="waiting_confirm",
            current_step=1,
            pending_command="tail -n 100 app.log",
            pending_target="dev-01",
        )
        await add_task_event(
            db,
            task["id"],
            "sess_task",
            "chat",
            "command_preview",
            status="waiting_confirm",
            step_index=1,
            content="tail -n 100 app.log",
            payload={"command": "tail -n 100 app.log"},
        )

        active = await get_session_tasks(db, "sess_task")
        assert active[0]["status"] == "waiting_confirm"
        assert active[0]["pending_command"] == "tail -n 100 app.log"

        events = await get_task_events(db, task["id"])
        assert events[0]["type"] == "command_preview"
        assert events[0]["payload"]["command"] == "tail -n 100 app.log"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_session_tasks_can_be_enriched_with_events(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await db.execute(
            """
            INSERT INTO sessions (id, title, type, caller, source, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("sess_events", "事件恢复", "chat", "test", "web", "active", "now", "now"),
        )
        await db.commit()
        task = await create_task(db, "sess_events", "chat", title="多步任务")
        await add_task_event(
            db,
            task["id"],
            "sess_events",
            "chat",
            "task_step",
            status="running",
            step_index=1,
            content="执行中",
        )

        tasks = await get_session_tasks(db, "sess_events")
        tasks[0]["events"] = await get_task_events(db, tasks[0]["id"])

        assert tasks[0]["events"][0]["type"] == "task_step"
        assert tasks[0]["events"][0]["status"] == "running"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_task_events_preserve_insert_order_with_same_timestamp(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await db.execute(
            """
            INSERT INTO sessions (id, title, type, caller, source, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("sess_event_order", "事件顺序", "chat", "test", "web", "active", "now", "now"),
        )
        await db.commit()
        task = await create_task(db, "sess_event_order", "chat", title="同秒事件")
        await add_task_event(db, task["id"], "sess_event_order", "chat", "turn_state", content="正在思考")
        await add_task_event(db, task["id"], "sess_event_order", "chat", "command_preview", content="df -h")
        await add_task_event(db, task["id"], "sess_event_order", "chat", "execution_result", content="ok")

        await db.execute("UPDATE agent_task_events SET created_at = ?", ("2026-07-09T10:00:00",))
        await db.commit()

        events = await get_task_events(db, task["id"])

        assert [event["type"] for event in events] == ["turn_state", "command_preview", "execution_result"]
    finally:
        await db.close()
