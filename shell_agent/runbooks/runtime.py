"""Shared state machine for registered deterministic deployment runbooks."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Protocol
from uuid import uuid4

from .models import (
    ArtifactSnapshot,
    DeploymentPlan,
    ExecutionRequest,
    ExecutionResult,
    PlanStep,
    RunConflictError,
    RunStatus,
    ServiceProfileSnapshot,
    StepPhase,
)
from .registry import RunbookRegistry, default_runbook_registry
from .storage import RunbookStorage


class DeploymentExecutor(Protocol):
    """Injected boundary; production adapters may use SSH/SFTP later.

    The runtime itself imports no SSH library. Tests provide an in-memory fake,
    so state-machine tests can never contact a real server.
    """

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


class DeploymentRunbookRuntime:
    def __init__(
        self,
        storage: RunbookStorage,
        executor: DeploymentExecutor,
        registry: RunbookRegistry | None = None,
    ):
        self.storage = storage
        self.executor = executor
        self.registry = registry or default_runbook_registry()

    async def create(
        self,
        *,
        request_id: str,
        session_id: str,
        service: ServiceProfileSnapshot,
        artifact: ArtifactSnapshot,
    ) -> tuple[dict, bool]:
        if artifact.session_id != session_id:
            raise ValueError("制品不属于当前会话")
        run_id = f"deprun_{uuid4().hex[:20]}"
        template = self.registry.resolve(service, artifact)
        plan = template.build_plan(run_id=run_id, service=service, artifact=artifact)
        return await self.storage.create_run(
            request_id=request_id, session_id=session_id, plan=plan
        )

    async def get(self, run_id: str, *, include_history: bool = True) -> dict:
        run = await self._require_run(run_id)
        if include_history:
            run["steps"] = await self.storage.list_steps(run_id)
            run["events"] = await self.storage.list_events(run_id)
        return run

    async def list(
        self,
        *,
        session_id: str | None = None,
        statuses: tuple[RunStatus, ...] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        return await self.storage.list_runs(
            session_id=session_id, statuses=statuses, limit=limit
        )

    async def cancel(self, run_id: str) -> dict:
        """Cancel only work that cannot have an in-flight remote side effect.

        Executing runs require an executor-specific stop/reconciliation flow;
        changing their status to canceled would falsely imply the remote action
        stopped.
        """
        run = await self._require_run(run_id)
        current = RunStatus(run["status"])
        cancelable = {
            RunStatus.CREATED,
            RunStatus.WAITING_PLAN_CONFIRM,
            RunStatus.CONFIRMED,
        }
        if current not in cancelable:
            raise RunConflictError("当前部署状态不能安全取消，需先核对远端状态")
        if not await self.storage.transition(
            run_id,
            expected=current,
            new=RunStatus.CANCELED,
            message="用户取消部署任务",
            completed=True,
        ):
            raise RunConflictError("部署任务状态已变化")
        return await self._require_run(run_id)

    async def prepare(self, run_id: str) -> dict:
        run = await self._require_run(run_id)
        if not await self.storage.transition(
            run_id,
            expected=RunStatus.CREATED,
            new=RunStatus.PRECHECK_RUNNING,
            message="开始执行只读部署前检查",
        ):
            raise RunConflictError("部署任务已被其他请求推进")
        plan = self._plan_from_run(run)
        for step in plan.steps_for(StepPhase.PRECHECK):
            result = await self._execute_step(run, step)
            if not result.success:
                await self.storage.transition(
                    run_id,
                    expected=RunStatus.PRECHECK_RUNNING,
                    new=RunStatus.PRECHECK_FAILED,
                    error=self._result_error(result),
                    message=f"部署前检查失败：{step.name}",
                    completed=True,
                )
                return await self._require_run(run_id)
        changed = await self.storage.transition(
            run_id,
            expected=RunStatus.PRECHECK_RUNNING,
            new=RunStatus.WAITING_PLAN_CONFIRM,
            message="部署前检查通过，等待确认冻结方案",
        )
        if not changed:  # pragma: no cover - defensive single-owner invariant
            raise RunConflictError("部署任务状态在检查期间发生变化")
        return await self._require_run(run_id)

    async def confirm_plan(
        self, run_id: str, *, plan_hash: str, confirmed_by: str
    ) -> dict:
        if not confirmed_by.strip():
            raise ValueError("确认人不能为空")
        if not await self.storage.confirm_plan(
            run_id, plan_hash=plan_hash, confirmed_by=confirmed_by
        ):
            raise RunConflictError("方案已确认、状态已变化或 plan_hash 不匹配")
        return await self._require_run(run_id)

    async def reject_plan(self, run_id: str) -> dict:
        if not await self.storage.transition(
            run_id,
            expected=RunStatus.WAITING_PLAN_CONFIRM,
            new=RunStatus.PLAN_REJECTED,
            message="用户拒绝部署方案",
            completed=True,
        ):
            raise RunConflictError("部署方案已处理")
        return await self._require_run(run_id)

    async def execute(self, run_id: str) -> dict:
        run = await self._require_run(run_id)
        if run["confirmed_plan_hash"] != run["plan_hash"]:
            raise RunConflictError("冻结方案尚未被精确确认")
        if not await self.storage.transition(
            run_id,
            expected=RunStatus.CONFIRMED,
            new=RunStatus.LOCK_ACQUIRING,
            message="正在获取服务部署锁",
        ):
            raise RunConflictError("部署任务已开始或状态已变化")
        locked = await self.storage.acquire_service_lock(
            service_key=run["service_key"],
            run_id=run_id,
            target=run["target"],
            metadata={"session_id": run["session_id"], "plan_hash": run["plan_hash"]},
        )
        if not locked:
            await self.storage.transition(
                run_id,
                expected=RunStatus.LOCK_ACQUIRING,
                new=RunStatus.LOCK_CONFLICT,
                error="同一服务已有部署任务",
                message="服务部署锁冲突",
                completed=True,
            )
            return await self._require_run(run_id)
        if not await self.storage.transition(
            run_id,
            expected=RunStatus.LOCK_ACQUIRING,
            new=RunStatus.LOCKED,
            message="已获取服务部署锁",
        ):
            # A process restart can race here. Keep the lock as evidence rather
            # than releasing it based on an uncertain state.
            raise RunConflictError("获取部署锁后任务状态发生变化")

        plan = self._plan_from_run(run)
        execute_steps = plan.steps_for(StepPhase.EXECUTE)
        for step in execute_steps:
            await self._enter_step_status(run_id, step)
            if step.mutates_live:
                await self.storage.mark_mutation_started(run_id)
            result = await self._execute_step(run, step)
            if not result.success:
                return await self._fail_execution(run, result, step)
            if step.action == "verify_staged_artifact":
                await self._must_transition(
                    run_id,
                    RunStatus.STAGING_UPLOAD,
                    RunStatus.ARTIFACT_VERIFIED,
                    "暂存制品校验通过",
                )

        await self._must_transition(
            run_id,
            RunStatus.STARTING,
            RunStatus.POSTCHECK_RUNNING,
            "服务已启动，开始部署后验证",
        )
        for step in plan.steps_for(StepPhase.POSTCHECK):
            result = await self._execute_step(run, step)
            if not result.success:
                return await self._fail_execution(run, result, step)

        # There is intentionally no path to SUCCEEDED before every postcheck
        # step has persisted a successful result.
        await self._must_transition(
            run_id,
            RunStatus.POSTCHECK_RUNNING,
            RunStatus.SUCCEEDED,
            "全部部署后检查通过",
        )
        await self._must_transition(
            run_id,
            RunStatus.SUCCEEDED,
            RunStatus.FINALIZING,
            "正在完成部署任务",
        )
        released = await self.storage.release_service_lock(
            service_key=run["service_key"], run_id=run_id
        )
        if not released:
            await self.storage.transition(
                run_id,
                expected=RunStatus.FINALIZING,
                new=RunStatus.MANUAL_INTERVENTION,
                error="部署成功但服务锁状态异常",
                message="部署锁释放失败，需要人工核对",
                completed=True,
            )
            return await self._require_run(run_id)
        template = self.registry.get(plan.runbook_id)
        await self.storage.transition(
            run_id,
            expected=RunStatus.FINALIZING,
            new=RunStatus.COMPLETED,
            result_summary=template.success_summary,
            message="部署完成",
            completed=True,
        )
        return await self._require_run(run_id)

    async def confirm_rollback(self, run_id: str, *, confirmed_by: str) -> dict:
        if not await self.storage.confirm_rollback(run_id, confirmed_by=confirmed_by):
            raise RunConflictError("回滚已确认或任务状态已变化")
        return await self._require_run(run_id)

    async def rollback(self, run_id: str) -> dict:
        run = await self._require_run(run_id)
        if not await self.storage.transition(
            run_id,
            expected=RunStatus.ROLLBACK_CONFIRMED,
            new=RunStatus.ROLLBACK_RUNNING,
            message="开始执行已确认的回滚方案",
        ):
            raise RunConflictError("回滚尚未确认或已开始")
        plan = self._plan_from_run(run)
        for step in plan.steps_for(StepPhase.ROLLBACK):
            result = await self._execute_step(run, step)
            if not result.success:
                return await self._rollback_failed(run, result, step)
        await self._must_transition(
            run_id,
            RunStatus.ROLLBACK_RUNNING,
            RunStatus.ROLLBACK_POSTCHECK,
            "回滚动作已完成，开始验证旧版本",
        )
        for step in plan.steps_for(StepPhase.ROLLBACK_POSTCHECK):
            result = await self._execute_step(run, step)
            if not result.success:
                return await self._rollback_failed(run, result, step)
        released = await self.storage.release_service_lock(
            service_key=run["service_key"], run_id=run_id
        )
        if not released:
            await self.storage.transition(
                run_id,
                expected=RunStatus.ROLLBACK_POSTCHECK,
                new=RunStatus.ROLLBACK_FAILED,
                error="旧版本已验证，但部署锁状态异常",
                message="回滚后部署锁释放失败",
            )
            await self.storage.transition(
                run_id,
                expected=RunStatus.ROLLBACK_FAILED,
                new=RunStatus.MANUAL_INTERVENTION,
                error="旧版本已验证，但部署锁状态异常",
                message="需要人工核对部署锁",
                completed=True,
            )
            return await self._require_run(run_id)
        template = self.registry.get(plan.runbook_id)
        changed = await self.storage.transition(
            run_id,
            expected=RunStatus.ROLLBACK_POSTCHECK,
            new=RunStatus.ROLLED_BACK,
            result_summary=template.rollback_summary,
            message="旧版本恢复并验证通过",
            completed=True,
        )
        if not changed:  # pragma: no cover - defensive single-owner invariant
            raise RunConflictError("回滚完成状态写入冲突")
        return await self._require_run(run_id)

    async def _enter_step_status(self, run_id: str, step: PlanStep) -> None:
        transitions = {
            "stage_upload": (RunStatus.LOCKED, RunStatus.STAGING_UPLOAD),
            "backup_current": (RunStatus.ARTIFACT_VERIFIED, RunStatus.BACKUP_RUNNING),
            "stop_service": (RunStatus.BACKUP_RUNNING, RunStatus.STOPPING),
            "switch_artifact": (RunStatus.STOPPING, RunStatus.SWITCHING),
            "start_service": (RunStatus.SWITCHING, RunStatus.STARTING),
        }
        state_change = transitions.get(step.action)
        if state_change:
            await self._must_transition(
                run_id, state_change[0], state_change[1], f"开始：{step.name}"
            )

    async def _execute_step(self, run: dict, step: PlanStep) -> ExecutionResult:
        if not await self.storage.start_step(run["id"], step.id):
            raise RunConflictError(f"步骤已执行或状态异常: {step.id}")
        request = ExecutionRequest(
            run_id=run["id"],
            session_id=run["session_id"],
            service_id=run["service_id"],
            target=run["target"],
            step=step,
            runbook_id=str(run.get("runbook_id") or ""),
        )
        try:
            result = await self.executor.execute(request)
        except Exception as exc:  # executor errors are persisted, never guessed
            result = ExecutionResult(success=False, stderr=str(exc))
        await self.storage.finish_step(
            run["id"],
            step.id,
            result=result,
            error="" if result.success else self._result_error(result),
        )
        return result

    async def _fail_execution(
        self, run: dict, result: ExecutionResult, step: PlanStep
    ) -> dict:
        current = RunStatus((await self._require_run(run["id"]))["status"])
        mutation_started = bool((await self._require_run(run["id"]))["mutation_started"])
        if mutation_started:
            await self.storage.transition(
                run["id"],
                expected=current,
                new=RunStatus.ROLLBACK_REQUIRED,
                error=self._result_error(result),
                message=f"步骤失败，等待确认回滚：{step.name}",
            )
        else:
            await self.storage.transition(
                run["id"],
                expected=current,
                new=RunStatus.STEP_FAILED,
                error=self._result_error(result),
                message=f"部署尚未改变服务，步骤失败：{step.name}",
                completed=True,
            )
            await self.storage.release_service_lock(
                service_key=run["service_key"], run_id=run["id"]
            )
        return await self._require_run(run["id"])

    async def _rollback_failed(
        self, run: dict, result: ExecutionResult, step: PlanStep
    ) -> dict:
        current = RunStatus((await self._require_run(run["id"]))["status"])
        await self.storage.transition(
            run["id"],
            expected=current,
            new=RunStatus.ROLLBACK_FAILED,
            error=self._result_error(result),
            message=f"回滚失败：{step.name}",
        )
        await self.storage.transition(
            run["id"],
            expected=RunStatus.ROLLBACK_FAILED,
            new=RunStatus.MANUAL_INTERVENTION,
            error=self._result_error(result),
            message="回滚未能恢复服务，保留部署锁并要求人工介入",
            completed=True,
        )
        return await self._require_run(run["id"])

    async def _must_transition(
        self,
        run_id: str,
        expected: RunStatus,
        new: RunStatus,
        message: str,
    ) -> None:
        if not await self.storage.transition(
            run_id, expected=expected, new=new, message=message
        ):
            raise RunConflictError(f"状态迁移冲突: {expected.value} -> {new.value}")

    async def _require_run(self, run_id: str) -> dict:
        run = await self.storage.get_run(run_id)
        if not run:
            raise KeyError(f"部署任务不存在: {run_id}")
        return run

    @staticmethod
    def _result_error(result: ExecutionResult) -> str:
        return (result.stderr or result.stdout or "执行器返回失败").strip()[:4000]

    @staticmethod
    def _plan_from_run(run: dict) -> DeploymentPlan:
        data = run["plan_json"]
        service_data = data["service"]
        artifact_data = data["artifact"]
        service = ServiceProfileSnapshot(
            **{
                **service_data,
                "artifact_type": service_data.get("artifact_type", "jar"),
                "runtime": service_data.get("runtime", ""),
                "ports": tuple(service_data.get("ports", [])),
            }
        )
        artifact = ArtifactSnapshot(**artifact_data)
        from .models import PlanStep, RiskLevel  # keep decoder close to persistence

        steps = tuple(
            PlanStep(
                id=item["id"],
                name=item["name"],
                phase=StepPhase(item["phase"]),
                action=item["action"],
                risk_level=RiskLevel(item["risk_level"]),
                mutates_live=bool(item.get("mutates_live")),
                arguments=item.get("arguments", {}),
            )
            for item in data["steps"]
        )
        plan = DeploymentPlan(
            runbook_id=data["runbook_id"],
            runbook_version=data["runbook_version"],
            run_id=data["run_id"],
            service=service,
            artifact=artifact,
            steps=steps,
        )
        persisted_hash = sha256(
            json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if persisted_hash != run["plan_hash"]:
            raise RuntimeError("持久化部署方案哈希校验失败")
        return plan


class SingleJavaJarDeploymentRuntime(DeploymentRunbookRuntime):
    """Backward-compatible name for callers from the original JAR-only API."""
