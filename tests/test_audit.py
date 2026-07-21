import pytest

from shell_agent.core.models import AuditRecord
from shell_agent.safety.audit import query_audit, write_audit
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
