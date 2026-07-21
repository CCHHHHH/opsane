from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json

import aiosqlite
import pytest

from shell_agent.runbooks import (
    build_single_java_jar_plan,
    DeploymentRunbookRuntime,
    ExecutionRequest,
    ExecutionResult,
    RunConflictError,
    RunStatus,
    RunbookStorage,
    SingleJavaJarDeploymentRuntime,
)

from .test_runbook_models import (
    artifact_snapshot,
    service_snapshot,
    tomcat_service_snapshot,
    war_artifact_snapshot,
)


@dataclass
class FakeDeploymentExecutor:
    fail_steps: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request.step.id)
        if request.step.id in self.fail_steps:
            return ExecutionResult(
                success=False,
                exit_code=1,
                stderr=f"injected failure: {request.step.id}",
            )
        return ExecutionResult(
            success=True,
            exit_code=0,
            stdout=f"ok: {request.step.id}",
            details={"fake": True},
        )


async def build_runtime(tmp_path, *, fail_steps: set[str] | None = None):
    db = await aiosqlite.connect(tmp_path / "runtime.db")
    storage = RunbookStorage(db)
    await storage.initialize()
    fake = FakeDeploymentExecutor(fail_steps or set())
    runtime = SingleJavaJarDeploymentRuntime(storage, fake)
    return db, storage, fake, runtime


def test_generic_runtime_decodes_legacy_jar_plan_without_new_profile_fields() -> None:
    plan = build_single_java_jar_plan(
        run_id="legacy_jar_run",
        service=service_snapshot(),
        artifact=artifact_snapshot(),
    )
    persisted = json.loads(plan.canonical_json())
    persisted["service"].pop("artifact_type")
    persisted["service"].pop("runtime")
    plan_hash = sha256(
        json.dumps(
            persisted,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    restored = DeploymentRunbookRuntime._plan_from_run(
        {"plan_json": persisted, "plan_hash": plan_hash}
    )

    assert restored.runbook_id == "single_java_jar_deploy"
    assert restored.service.artifact_type == "jar"
    assert restored.service.runtime == ""


@pytest.mark.asyncio
async def test_generic_runtime_selects_registered_war_template(tmp_path) -> None:
    db = await aiosqlite.connect(tmp_path / "war-runtime.db")
    storage = RunbookStorage(db)
    await storage.initialize()
    fake = FakeDeploymentExecutor()
    runtime = DeploymentRunbookRuntime(storage, fake)
    try:
        run, created = await runtime.create(
            request_id="req_war",
            session_id="session_001",
            service=tomcat_service_snapshot(),
            artifact=war_artifact_snapshot(),
        )
        assert created
        assert run["runbook_id"] == "single_tomcat_war_deploy"

        prepared = await runtime.prepare(run["id"])
        await runtime.confirm_plan(
            run["id"], plan_hash=prepared["plan_hash"], confirmed_by="alice"
        )
        completed = await runtime.execute(run["id"])

        assert completed["status"] == RunStatus.COMPLETED.value
        assert "WAR" in completed["result_summary"]
        assert "archive_exploded_context" in fake.calls
    finally:
        await db.close()


async def prepared_run(runtime: SingleJavaJarDeploymentRuntime, *, request_id="req_1"):
    run, created = await runtime.create(
        request_id=request_id,
        session_id="session_001",
        service=service_snapshot(),
        artifact=artifact_snapshot(),
    )
    assert created
    run = await runtime.prepare(run["id"])
    assert run["status"] == RunStatus.WAITING_PLAN_CONFIRM.value
    return run


@pytest.mark.asyncio
async def test_success_requires_all_postchecks_and_releases_lock(tmp_path) -> None:
    db, storage, fake, runtime = await build_runtime(tmp_path)
    try:
        run = await prepared_run(runtime)
        run = await runtime.confirm_plan(
            run["id"], plan_hash=run["plan_hash"], confirmed_by="alice"
        )
        assert run["status"] == RunStatus.CONFIRMED.value
        run = await runtime.execute(run["id"])

        assert run["status"] == RunStatus.COMPLETED.value
        assert run["completed_at"]
        assert fake.calls[-3:] == [
            "postcheck_status",
            "postcheck_health",
            "postcheck_artifact",
        ]
        steps = await storage.list_steps(run["id"])
        postchecks = [step for step in steps if step["phase"] == "postcheck"]
        assert postchecks
        assert {step["status"] for step in postchecks} == {"success"}
        assert await storage.get_service_lock(run["service_key"]) is None

        statuses = [event["status"] for event in await storage.list_events(run["id"])]
        assert statuses.index(RunStatus.POSTCHECK_RUNNING.value) < statuses.index(
            RunStatus.SUCCEEDED.value
        ) < statuses.index(RunStatus.COMPLETED.value)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_plan_confirmation_is_cas_and_execute_cannot_double_start(tmp_path) -> None:
    db, _storage, _fake, runtime = await build_runtime(tmp_path)
    try:
        run = await prepared_run(runtime)
        with pytest.raises(RunConflictError, match="plan_hash"):
            await runtime.confirm_plan(
                run["id"], plan_hash="0" * 64, confirmed_by="alice"
            )
        await runtime.confirm_plan(
            run["id"], plan_hash=run["plan_hash"], confirmed_by="alice"
        )
        with pytest.raises(RunConflictError, match="方案"):
            await runtime.confirm_plan(
                run["id"], plan_hash=run["plan_hash"], confirmed_by="alice"
            )

        listed = await runtime.list(session_id="session_001")
        assert [item["id"] for item in listed] == [run["id"]]
        detail = await runtime.get(run["id"])
        assert len(detail["steps"]) > 10
        assert detail["events"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_is_allowed_only_before_remote_execution(tmp_path) -> None:
    db, _storage, _fake, runtime = await build_runtime(tmp_path)
    try:
        run = await prepared_run(runtime, request_id="req_cancel")
        canceled = await runtime.cancel(run["id"])
        assert canceled["status"] == RunStatus.CANCELED.value
        assert canceled["completed_at"]
        with pytest.raises(RunConflictError, match="不能安全取消"):
            await runtime.cancel(run["id"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_precheck_failure_never_acquires_lock_or_mutates_service(tmp_path) -> None:
    db, storage, fake, runtime = await build_runtime(
        tmp_path, fail_steps={"precheck_disk"}
    )
    try:
        run, _ = await runtime.create(
            request_id="req_precheck_fail",
            session_id="session_001",
            service=service_snapshot(),
            artifact=artifact_snapshot(),
        )
        run = await runtime.prepare(run["id"])
        assert run["status"] == RunStatus.PRECHECK_FAILED.value
        assert run["mutation_started"] is False
        assert fake.calls == ["precheck_host", "precheck_layout", "precheck_disk"]
        assert await storage.get_service_lock(run["service_key"]) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upload_failure_stops_before_service_mutation_and_unlocks(tmp_path) -> None:
    db, storage, _fake, runtime = await build_runtime(
        tmp_path, fail_steps={"stage_upload"}
    )
    try:
        run = await prepared_run(runtime)
        await runtime.confirm_plan(
            run["id"], plan_hash=run["plan_hash"], confirmed_by="alice"
        )
        run = await runtime.execute(run["id"])
        assert run["status"] == RunStatus.STEP_FAILED.value
        assert run["mutation_started"] is False
        assert await storage.get_service_lock(run["service_key"]) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_health_check_requires_confirmed_rollback(tmp_path) -> None:
    db, storage, fake, runtime = await build_runtime(
        tmp_path, fail_steps={"postcheck_health"}
    )
    try:
        run = await prepared_run(runtime)
        await runtime.confirm_plan(
            run["id"], plan_hash=run["plan_hash"], confirmed_by="alice"
        )
        run = await runtime.execute(run["id"])
        assert run["status"] == RunStatus.ROLLBACK_REQUIRED.value
        assert run["mutation_started"] is True
        assert await storage.get_service_lock(run["service_key"]) is not None
        assert "postcheck_artifact" not in fake.calls

        with pytest.raises(RunConflictError, match="尚未确认"):
            await runtime.rollback(run["id"])
        await runtime.confirm_rollback(run["id"], confirmed_by="alice")
        run = await runtime.rollback(run["id"])
        assert run["status"] == RunStatus.ROLLED_BACK.value
        assert fake.calls[-5:] == [
            "rollback_stop",
            "rollback_restore",
            "rollback_start",
            "rollback_postcheck_status",
            "rollback_postcheck_health",
        ]
        assert await storage.get_service_lock(run["service_key"]) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rollback_failure_becomes_manual_intervention_and_keeps_lock(tmp_path) -> None:
    db, storage, _fake, runtime = await build_runtime(
        tmp_path, fail_steps={"start_service", "rollback_restore"}
    )
    try:
        run = await prepared_run(runtime)
        await runtime.confirm_plan(
            run["id"], plan_hash=run["plan_hash"], confirmed_by="alice"
        )
        run = await runtime.execute(run["id"])
        assert run["status"] == RunStatus.ROLLBACK_REQUIRED.value
        await runtime.confirm_rollback(run["id"], confirmed_by="alice")
        run = await runtime.rollback(run["id"])
        assert run["status"] == RunStatus.MANUAL_INTERVENTION.value
        assert await storage.get_service_lock(run["service_key"]) is not None
    finally:
        await db.close()
