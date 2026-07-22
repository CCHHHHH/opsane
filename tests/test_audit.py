import pytest

from shell_agent.core.models import AuditRecord
from shell_agent.safety.audit import count_audit, query_audit, write_audit
from shell_agent.storage.database import connect, init_db


@pytest.mark.asyncio
async def test_audit_write_and_query(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))

    try:
        await write_audit(
            db,
            AuditRecord(
                command="df -h",
                target="unit-host",
                target_env="test",
                executor="ssh",
                executed=False,
                source="test",
                caller="tester",
                session_id="sess_test",
                user_confirmed=False,
            ),
        )

        rows = await query_audit(db, target="unit-host", limit=5)
    finally:
        await db.close()

    assert len(rows) == 1
    assert rows[0]["command"] == "df -h"
    assert rows[0]["target"] == "unit-host"
    assert rows[0]["executed"] == 0
    assert rows[0]["user_confirmed"] == 0


@pytest.mark.asyncio
async def test_audit_query_supports_count_and_offset_pagination(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))

    try:
        for index in range(25):
            await write_audit(
                db,
                AuditRecord(
                    command=f"echo {index:02d}",
                    target="page-host",
                    target_env="test",
                    executor="ssh",
                    source="test",
                    caller="tester",
                    session_id="sess_page",
                    executed=True,
                    exit_code=0,
                    timestamp=f"2026-07-21T10:{index:02d}:00",
                ),
            )
        await write_audit(
            db,
            AuditRecord(
                command="other",
                target="other-host",
                target_env="test",
                executor="ssh",
                source="test",
                caller="tester",
                session_id="sess_other",
                executed=False,
            ),
        )

        total = await count_audit(db, target="page-host")
        second_page = await query_audit(
            db,
            target="page-host",
            limit=10,
            offset=10,
        )
    finally:
        await db.close()

    assert total == 25
    assert len(second_page) == 10
    assert [row["command"] for row in second_page] == [
        f"echo {index:02d}" for index in range(14, 4, -1)
    ]
