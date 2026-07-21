from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from shell_agent.runbooks import RunStatus, RunbookStorage, build_single_java_jar_plan

from .test_runbook_models import artifact_snapshot, service_snapshot


@pytest.mark.asyncio
async def test_plan_is_database_immutable_and_confirmation_is_exactly_once(tmp_path) -> None:
    db = await aiosqlite.connect(tmp_path / "deploy.db")
    storage = RunbookStorage(db)
    await storage.initialize()
    try:
        plan = build_single_java_jar_plan(
            run_id="deprun_immutable",
            service=service_snapshot(),
            artifact=artifact_snapshot(),
        )
        run, created = await storage.create_run(
            request_id="req_immutable", session_id="session_001", plan=plan
        )
        assert created is True
        assert run["plan_hash"] == plan.plan_hash

        with pytest.raises(aiosqlite.DatabaseError, match="immutable"):
            await db.execute(
                "UPDATE deployment_runs SET plan_hash = ? WHERE id = ?",
                ("b" * 64, plan.run_id),
            )
        await db.rollback()

        assert await storage.transition(
            plan.run_id,
            expected=RunStatus.CREATED,
            new=RunStatus.PRECHECK_RUNNING,
        )
        assert await storage.transition(
            plan.run_id,
            expected=RunStatus.PRECHECK_RUNNING,
            new=RunStatus.WAITING_PLAN_CONFIRM,
        )
        assert not await storage.confirm_plan(
            plan.run_id, plan_hash="c" * 64, confirmed_by="alice"
        )
        assert await storage.confirm_plan(
            plan.run_id, plan_hash=plan.plan_hash, confirmed_by="alice"
        )
        assert not await storage.confirm_plan(
            plan.run_id, plan_hash=plan.plan_hash, confirmed_by="alice"
        )

        confirmed = await storage.get_run(plan.run_id)
        assert confirmed is not None
        assert confirmed["status"] == RunStatus.CONFIRMED.value
        assert confirmed["confirmed_plan_hash"] == plan.plan_hash

        # Even a direct SQL write cannot claim success while postchecks are
        # still pending. This is stronger than relying on runtime call order.
        with pytest.raises(aiosqlite.DatabaseError, match="requires completed postchecks"):
            await db.execute(
                "UPDATE deployment_runs SET status = 'succeeded' WHERE id = ?",
                (plan.run_id,),
            )
        await db.rollback()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_service_lock_has_exactly_one_winner(tmp_path) -> None:
    path = tmp_path / "deploy-lock.db"
    db1 = await aiosqlite.connect(path)
    db2 = await aiosqlite.connect(path)
    storage1 = RunbookStorage(db1)
    storage2 = RunbookStorage(db2)
    await storage1.initialize()
    await storage2.initialize()
    try:
        plan1 = build_single_java_jar_plan(
            run_id="deprun_lock_1",
            service=service_snapshot(),
            artifact=artifact_snapshot(),
        )
        plan2 = build_single_java_jar_plan(
            run_id="deprun_lock_2",
            service=service_snapshot(),
            artifact=artifact_snapshot(file_id="file_002"),
        )
        run1, _ = await storage1.create_run(
            request_id="req_lock_1", session_id="session_001", plan=plan1
        )
        run2, _ = await storage1.create_run(
            request_id="req_lock_2", session_id="session_001", plan=plan2
        )
        assert run1["service_key"] == run2["service_key"]

        winners = await asyncio.gather(
            storage1.acquire_service_lock(
                service_key=run1["service_key"],
                run_id=run1["id"],
                target=run1["target"],
            ),
            storage2.acquire_service_lock(
                service_key=run2["service_key"],
                run_id=run2["id"],
                target=run2["target"],
            ),
        )
        assert sorted(winners) == [False, True]
        lock = await storage1.get_service_lock(run1["service_key"])
        assert lock is not None
        assert lock["run_id"] in {run1["id"], run2["id"]}
    finally:
        await db1.close()
        await db2.close()


@pytest.mark.asyncio
async def test_startup_reconciliation_only_marks_executing_runs_unknown(tmp_path) -> None:
    db = await aiosqlite.connect(tmp_path / "reconcile.db")
    storage = RunbookStorage(db)
    await storage.initialize()
    try:
        waiting_plan = build_single_java_jar_plan(
            run_id="deprun_waiting",
            service=service_snapshot(),
            artifact=artifact_snapshot(),
        )
        active_plan = build_single_java_jar_plan(
            run_id="deprun_active",
            service=service_snapshot(),
            artifact=artifact_snapshot(file_id="file_active"),
        )
        await storage.create_run(
            request_id="req_waiting", session_id="session_001", plan=waiting_plan
        )
        active, _ = await storage.create_run(
            request_id="req_active", session_id="session_001", plan=active_plan
        )

        for run_id in (waiting_plan.run_id, active_plan.run_id):
            assert await storage.transition(
                run_id,
                expected=RunStatus.CREATED,
                new=RunStatus.PRECHECK_RUNNING,
            )
            assert await storage.transition(
                run_id,
                expected=RunStatus.PRECHECK_RUNNING,
                new=RunStatus.WAITING_PLAN_CONFIRM,
            )
        assert await storage.confirm_plan(
            active_plan.run_id,
            plan_hash=active_plan.plan_hash,
            confirmed_by="alice",
        )
        assert await storage.transition(
            active_plan.run_id,
            expected=RunStatus.CONFIRMED,
            new=RunStatus.LOCK_ACQUIRING,
        )
        assert await storage.acquire_service_lock(
            service_key=active["service_key"],
            run_id=active_plan.run_id,
            target=active["target"],
        )
        assert await storage.transition(
            active_plan.run_id,
            expected=RunStatus.LOCK_ACQUIRING,
            new=RunStatus.LOCKED,
        )

        reconciled = await storage.reconcile_interrupted_runs()
        assert reconciled == [active_plan.run_id]
        waiting = await storage.get_run(waiting_plan.run_id)
        active_after = await storage.get_run(active_plan.run_id)
        assert waiting and waiting["status"] == RunStatus.WAITING_PLAN_CONFIRM.value
        assert active_after and active_after["status"] == RunStatus.UNKNOWN.value
        # Unknown remote state deliberately retains the lock for human review.
        assert await storage.get_service_lock(active["service_key"]) is not None
    finally:
        await db.close()
