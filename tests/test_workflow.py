import pytest
import pytest_asyncio

from shell_agent.core.models import ConfirmMode, ExecutionResult, PendingCommand
from shell_agent.safety import workflow
from shell_agent.safety.audit import query_audit
from shell_agent.safety.workflow import execute_with_confirmation
from shell_agent.storage.database import connect, init_db


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, command: PendingCommand) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=1,
        )


def _command(actual_command: str) -> PendingCommand:
    return PendingCommand(
        raw=f"ssh unit-host '{actual_command}'",
        target="unit-host",
        target_env="test",
        executor="ssh",
        actual_command=actual_command,
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    connection = await connect(str(db_path))
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_auto_safe_executes_safe_command_without_prompt(db, monkeypatch) -> None:
    executor = FakeExecutor()

    async def fail_if_prompted() -> bool:
        raise AssertionError("auto_safe safe command should not prompt")

    monkeypatch.setattr(workflow, "confirm_interactive", fail_if_prompted)

    result = await execute_with_confirmation(
        db=db,
        executor=executor,
        command=_command("df -h"),
        session_id="sess_safe",
        caller="tester",
        source="test",
        confirm_mode=ConfirmMode.AUTO_SAFE,
    )

    rows = await query_audit(db, target="unit-host", limit=5)
    assert result is not None
    assert executor.calls == 1
    assert rows[0]["executed"] == 1
    assert rows[0]["user_confirmed"] == 1


@pytest.mark.asyncio
async def test_auto_safe_prompts_for_dangerous_command(db, monkeypatch) -> None:
    executor = FakeExecutor()

    async def decline() -> bool:
        return False

    monkeypatch.setattr(workflow, "confirm_interactive", decline)

    result = await execute_with_confirmation(
        db=db,
        executor=executor,
        command=_command("systemctl restart order-service"),
        session_id="sess_dangerous",
        caller="tester",
        source="test",
        confirm_mode=ConfirmMode.AUTO_SAFE,
    )

    rows = await query_audit(db, target="unit-host", limit=5)
    assert result is None
    assert executor.calls == 0
    assert rows[0]["executed"] == 0
    assert rows[0]["user_confirmed"] == 0


@pytest.mark.asyncio
async def test_auto_safe_prompts_for_unapproved_compound_command(db, monkeypatch) -> None:
    executor = FakeExecutor()

    async def decline() -> bool:
        return False

    monkeypatch.setattr(workflow, "confirm_interactive", decline)

    result = await execute_with_confirmation(
        db=db,
        executor=executor,
        command=_command("df -h; useradd backdoor"),
        session_id="sess_compound",
        caller="tester",
        source="test",
        confirm_mode=ConfirmMode.AUTO_SAFE,
    )

    rows = await query_audit(db, target="unit-host", limit=5)
    assert result is None
    assert executor.calls == 0
    assert rows[0]["executed"] == 0
    assert rows[0]["user_confirmed"] == 0


@pytest.mark.asyncio
async def test_dry_run_never_executes(db, monkeypatch) -> None:
    executor = FakeExecutor()

    async def fail_if_prompted() -> bool:
        raise AssertionError("dry_run should not prompt")

    monkeypatch.setattr(workflow, "confirm_interactive", fail_if_prompted)

    result = await execute_with_confirmation(
        db=db,
        executor=executor,
        command=_command("df -h"),
        session_id="sess_dry",
        caller="tester",
        source="test",
        confirm_mode=ConfirmMode.DRY_RUN,
    )

    rows = await query_audit(db, target="unit-host", limit=5)
    assert result is None
    assert executor.calls == 0
    assert rows[0]["executed"] == 0
    assert rows[0]["user_confirmed"] is None


@pytest.mark.asyncio
async def test_auto_confirm_blocks_critical_command(db, monkeypatch) -> None:
    executor = FakeExecutor()

    async def fail_if_prompted() -> bool:
        raise AssertionError("critical auto_confirm should be blocked, not prompted")

    monkeypatch.setattr(workflow, "confirm_interactive", fail_if_prompted)

    result = await execute_with_confirmation(
        db=db,
        executor=executor,
        command=_command("rm -rf /var/lib/app/cache"),
        session_id="sess_critical",
        caller="tester",
        source="test",
        auto_confirm=True,
    )

    rows = await query_audit(db, target="unit-host", limit=5)
    assert result is None
    assert executor.calls == 0
    assert rows[0]["executed"] == 0
    assert rows[0]["user_confirmed"] is None
