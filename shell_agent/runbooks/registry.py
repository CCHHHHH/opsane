"""Registry for deterministic deployment runbook templates."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from .models import (
    ArtifactSnapshot,
    DeploymentPlan,
    DeploymentValidationError,
    ServiceProfileSnapshot,
    build_single_java_jar_plan,
    build_single_tomcat_war_plan,
)


class RunbookTemplate(Protocol):
    runbook_id: str
    version: str
    artifact_type: str
    success_summary: str
    rollback_summary: str

    def supports(self, service: ServiceProfileSnapshot) -> bool: ...

    def build_plan(
        self,
        *,
        run_id: str,
        service: ServiceProfileSnapshot,
        artifact: ArtifactSnapshot,
    ) -> DeploymentPlan: ...


PlanBuilder = Callable[..., DeploymentPlan]


@dataclass(frozen=True, slots=True)
class DeterministicRunbookTemplate:
    runbook_id: str
    version: str
    artifact_type: str
    builder: PlanBuilder
    runtimes: frozenset[str] = frozenset()
    success_summary: str = "制品已替换，服务状态、健康状态和制品校验均通过"
    rollback_summary: str = "部署前版本已恢复，状态和健康检查均通过"

    def supports(self, service: ServiceProfileSnapshot) -> bool:
        if service.artifact_type.strip().lower() != self.artifact_type:
            return False
        runtime = service.runtime.strip().lower()
        return not self.runtimes or not runtime or any(
            marker in runtime for marker in self.runtimes
        )

    def build_plan(
        self,
        *,
        run_id: str,
        service: ServiceProfileSnapshot,
        artifact: ArtifactSnapshot,
    ) -> DeploymentPlan:
        plan = self.builder(run_id=run_id, service=service, artifact=artifact)
        if plan.runbook_id != self.runbook_id or plan.runbook_version != self.version:
            raise RuntimeError("Runbook 模板元数据与冻结方案不一致")
        return plan


class RunbookRegistry:
    """Selects one explicit template; it never invents deployment steps."""

    def __init__(self, templates: Iterable[RunbookTemplate] = ()) -> None:
        self._templates: dict[str, RunbookTemplate] = {}
        for template in templates:
            self.register(template)

    def register(self, template: RunbookTemplate) -> None:
        if template.runbook_id in self._templates:
            raise ValueError(f"Runbook 已注册: {template.runbook_id}")
        self._templates[template.runbook_id] = template

    def get(self, runbook_id: str) -> RunbookTemplate:
        try:
            return self._templates[runbook_id]
        except KeyError as exc:
            raise DeploymentValidationError(
                f"未注册的部署 Runbook: {runbook_id}"
            ) from exc

    def resolve(
        self,
        service: ServiceProfileSnapshot,
        artifact: ArtifactSnapshot,
    ) -> RunbookTemplate:
        artifact_type = service.artifact_type.strip().lower()
        candidates = [
            template
            for template in self._templates.values()
            if template.artifact_type == artifact_type and template.supports(service)
        ]
        if not candidates:
            runtime = service.runtime.strip().lower() or "未指定"
            raise DeploymentValidationError(
                f"没有匹配的部署 Runbook：制品类型 {artifact_type or '未配置'}，运行方式 {runtime}"
            )
        if len(candidates) > 1:
            names = ", ".join(item.runbook_id for item in candidates)
            raise DeploymentValidationError(f"部署 Runbook 匹配不唯一: {names}")
        template = candidates[0]
        # Build-time validation remains authoritative.  Resolve only selects a
        # deterministic template and does not weaken its file contract.
        if not artifact.name.lower().endswith(f".{template.artifact_type}"):
            raise DeploymentValidationError(
                f"服务需要 {template.artifact_type.upper()} 制品，但选择的文件是 {artifact.name}"
            )
        return template

    def list_templates(self) -> tuple[RunbookTemplate, ...]:
        return tuple(self._templates.values())


def default_runbook_registry() -> RunbookRegistry:
    return RunbookRegistry(
        (
            DeterministicRunbookTemplate(
                runbook_id="single_java_jar_deploy",
                version="1.0.0",
                artifact_type="jar",
                runtimes=frozenset({"systemd", "standalone", "java"}),
                builder=build_single_java_jar_plan,
            ),
            DeterministicRunbookTemplate(
                runbook_id="single_tomcat_war_deploy",
                version="1.0.0",
                artifact_type="war",
                runtimes=frozenset({"tomcat"}),
                builder=build_single_tomcat_war_plan,
                success_summary="WAR 已替换，Tomcat 状态、应用健康状态和制品校验均通过",
                rollback_summary="部署前 WAR 和解压目录已恢复，Tomcat 健康检查通过",
            ),
        )
    )
