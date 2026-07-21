"""Domain models for durable, deterministic deployment runbooks.

Every supported deployment shape is represented by a frozen plan.  The
language model may select a registered template, but it never participates in
building or changing the resulting execution plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence


class DeploymentValidationError(ValueError):
    """The requested deployment cannot be represented safely."""


class RunConflictError(RuntimeError):
    """A compare-and-swap operation lost a race or used stale state."""


class RunStatus(StrEnum):
    CREATED = "created"
    PRECHECK_RUNNING = "precheck_running"
    WAITING_PLAN_CONFIRM = "waiting_plan_confirm"
    CONFIRMED = "confirmed"
    LOCK_ACQUIRING = "lock_acquiring"
    LOCKED = "locked"
    STAGING_UPLOAD = "staging_upload"
    ARTIFACT_VERIFIED = "artifact_verified"
    BACKUP_RUNNING = "backup_running"
    STOPPING = "stopping"
    SWITCHING = "switching"
    STARTING = "starting"
    POSTCHECK_RUNNING = "postcheck_running"
    SUCCEEDED = "succeeded"
    FINALIZING = "finalizing"
    COMPLETED = "completed"

    PRECHECK_FAILED = "precheck_failed"
    PLAN_REJECTED = "plan_rejected"
    LOCK_CONFLICT = "lock_conflict"
    STEP_FAILED = "step_failed"
    ROLLBACK_REQUIRED = "rollback_required"
    ROLLBACK_CONFIRMED = "rollback_confirmed"
    ROLLBACK_RUNNING = "rollback_running"
    ROLLBACK_POSTCHECK = "rollback_postcheck"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    MANUAL_INTERVENTION = "manual_intervention"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


class StepPhase(StrEnum):
    PRECHECK = "precheck"
    EXECUTE = "execute"
    POSTCHECK = "postcheck"
    ROLLBACK = "rollback"
    ROLLBACK_POSTCHECK = "rollback_postcheck"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class RiskLevel(StrEnum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"
    CRITICAL = "critical"


TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.PRECHECK_FAILED,
        RunStatus.PLAN_REJECTED,
        RunStatus.LOCK_CONFLICT,
        RunStatus.STEP_FAILED,
        RunStatus.ROLLED_BACK,
        RunStatus.MANUAL_INTERVENTION,
        RunStatus.CANCELED,
    }
)

# These statuses mean a previous process may have issued a remote operation.
# Startup reconciliation must never guess whether such an operation completed.
INTERRUPTIBLE_RUN_STATUSES = frozenset(
    {
        RunStatus.PRECHECK_RUNNING,
        RunStatus.LOCK_ACQUIRING,
        RunStatus.LOCKED,
        RunStatus.STAGING_UPLOAD,
        RunStatus.ARTIFACT_VERIFIED,
        RunStatus.BACKUP_RUNNING,
        RunStatus.STOPPING,
        RunStatus.SWITCHING,
        RunStatus.STARTING,
        RunStatus.POSTCHECK_RUNNING,
        RunStatus.SUCCEEDED,
        RunStatus.FINALIZING,
        RunStatus.ROLLBACK_RUNNING,
        RunStatus.ROLLBACK_POSTCHECK,
    }
)


_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.PRECHECK_RUNNING, RunStatus.CANCELED}),
    RunStatus.PRECHECK_RUNNING: frozenset(
        {RunStatus.WAITING_PLAN_CONFIRM, RunStatus.PRECHECK_FAILED, RunStatus.UNKNOWN}
    ),
    RunStatus.WAITING_PLAN_CONFIRM: frozenset(
        {RunStatus.CONFIRMED, RunStatus.PLAN_REJECTED, RunStatus.CANCELED}
    ),
    RunStatus.CONFIRMED: frozenset({RunStatus.LOCK_ACQUIRING, RunStatus.CANCELED}),
    RunStatus.LOCK_ACQUIRING: frozenset(
        {RunStatus.LOCKED, RunStatus.LOCK_CONFLICT, RunStatus.UNKNOWN}
    ),
    RunStatus.LOCKED: frozenset(
        {RunStatus.STAGING_UPLOAD, RunStatus.STEP_FAILED, RunStatus.UNKNOWN}
    ),
    RunStatus.STAGING_UPLOAD: frozenset(
        {RunStatus.ARTIFACT_VERIFIED, RunStatus.STEP_FAILED, RunStatus.UNKNOWN}
    ),
    RunStatus.ARTIFACT_VERIFIED: frozenset(
        {RunStatus.BACKUP_RUNNING, RunStatus.STEP_FAILED, RunStatus.UNKNOWN}
    ),
    RunStatus.BACKUP_RUNNING: frozenset(
        {RunStatus.STOPPING, RunStatus.STEP_FAILED, RunStatus.UNKNOWN}
    ),
    RunStatus.STOPPING: frozenset(
        {RunStatus.SWITCHING, RunStatus.ROLLBACK_REQUIRED, RunStatus.UNKNOWN}
    ),
    RunStatus.SWITCHING: frozenset(
        {RunStatus.STARTING, RunStatus.ROLLBACK_REQUIRED, RunStatus.UNKNOWN}
    ),
    RunStatus.STARTING: frozenset(
        {RunStatus.POSTCHECK_RUNNING, RunStatus.ROLLBACK_REQUIRED, RunStatus.UNKNOWN}
    ),
    RunStatus.POSTCHECK_RUNNING: frozenset(
        {RunStatus.SUCCEEDED, RunStatus.ROLLBACK_REQUIRED, RunStatus.UNKNOWN}
    ),
    RunStatus.SUCCEEDED: frozenset({RunStatus.FINALIZING}),
    RunStatus.FINALIZING: frozenset(
        {RunStatus.COMPLETED, RunStatus.MANUAL_INTERVENTION, RunStatus.UNKNOWN}
    ),
    RunStatus.ROLLBACK_REQUIRED: frozenset(
        {RunStatus.ROLLBACK_CONFIRMED, RunStatus.MANUAL_INTERVENTION}
    ),
    RunStatus.ROLLBACK_CONFIRMED: frozenset({RunStatus.ROLLBACK_RUNNING}),
    RunStatus.ROLLBACK_RUNNING: frozenset(
        {RunStatus.ROLLBACK_POSTCHECK, RunStatus.ROLLBACK_FAILED, RunStatus.UNKNOWN}
    ),
    RunStatus.ROLLBACK_POSTCHECK: frozenset(
        {RunStatus.ROLLED_BACK, RunStatus.ROLLBACK_FAILED, RunStatus.UNKNOWN}
    ),
    RunStatus.ROLLBACK_FAILED: frozenset({RunStatus.MANUAL_INTERVENTION}),
}


def transition_allowed(current: RunStatus, new: RunStatus) -> bool:
    return new in _TRANSITIONS.get(current, frozenset())


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    # The JSON round trip also rejects non-JSON arguments before a plan is
    # persisted and hashed.
    copied = json.loads(json.dumps(dict(value or {}), ensure_ascii=False))
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class ServiceProfileSnapshot:
    service_id: str
    service_name: str
    revision: int
    verification_status: str
    environment: str
    target: str
    deploy_dir: str
    artifact_path: str
    backup_dir: str
    start_cmd: str
    stop_cmd: str
    status_cmd: str
    artifact_type: str = "jar"
    runtime: str = ""
    health_url: str = ""
    ports: tuple[int, ...] = ()
    startup_timeout_seconds: int = 60

    def validate(self) -> None:
        required = {
            "service_id": self.service_id,
            "service_name": self.service_name,
            "target": self.target,
            "deploy_dir": self.deploy_dir,
            "artifact_path": self.artifact_path,
            "backup_dir": self.backup_dir,
            "start_cmd": self.start_cmd,
            "stop_cmd": self.stop_cmd,
            "status_cmd": self.status_cmd,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise DeploymentValidationError(
                f"服务画像缺少部署必填字段: {', '.join(missing)}"
            )
        if self.environment not in {"dev", "test"}:
            raise DeploymentValidationError("首版部署 Runbook 仅允许 dev/test 环境")
        if self.verification_status != "verified":
            raise DeploymentValidationError("服务画像尚未验证，禁止执行部署")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", self.artifact_type.strip().lower()):
            raise DeploymentValidationError("服务画像中的制品类型无效")
        for label, raw_path in (
            ("部署目录", self.deploy_dir),
            ("目标制品路径", self.artifact_path),
            ("备份目录", self.backup_dir),
        ):
            path = str(raw_path).strip()
            if (
                not path.startswith("/")
                or path != str(PurePosixPath(path))
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or bool(re.search(r"[;&|`$<>]", path))
            ):
                raise DeploymentValidationError(f"{label}必须是规范的绝对路径")
        if not self.health_url and not self.ports:
            raise DeploymentValidationError("必须配置健康检查 URL 或已验证监听端口")
        if self.startup_timeout_seconds <= 0:
            raise DeploymentValidationError("启动检查超时时间必须大于 0")
        if any(port < 1 or port > 65535 for port in self.ports):
            raise DeploymentValidationError("服务端口超出有效范围")

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "service_name": self.service_name,
            "revision": self.revision,
            "verification_status": self.verification_status,
            "environment": self.environment,
            "target": self.target,
            "deploy_dir": self.deploy_dir,
            "artifact_path": self.artifact_path,
            "backup_dir": self.backup_dir,
            "start_cmd": self.start_cmd,
            "stop_cmd": self.stop_cmd,
            "status_cmd": self.status_cmd,
            "artifact_type": self.artifact_type.strip().lower(),
            "runtime": self.runtime.strip().lower(),
            "health_url": self.health_url,
            "ports": list(self.ports),
            "startup_timeout_seconds": self.startup_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    file_id: str
    session_id: str
    name: str
    local_path: str
    size: int
    sha256: str

    def validate(self, *, expected_type: str = "") -> None:
        if not all((self.file_id, self.session_id, self.name, self.local_path)):
            raise DeploymentValidationError("制品快照字段不完整")
        normalized_type = expected_type.strip().lower()
        if normalized_type and not self.name.lower().endswith(f".{normalized_type}"):
            raise DeploymentValidationError(
                f"{normalized_type.upper()} 部署模板只接受 .{normalized_type} 制品"
            )
        if self.size <= 0:
            raise DeploymentValidationError("制品大小必须大于 0")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256):
            raise DeploymentValidationError("制品 SHA-256 非法")

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "session_id": self.session_id,
            "name": self.name,
            "local_path": self.local_path,
            "size": self.size,
            "sha256": self.sha256.lower(),
        }


def _validate_template_inputs(
    service: ServiceProfileSnapshot,
    artifact: ArtifactSnapshot,
    *,
    artifact_type: str,
) -> None:
    """Apply common validation plus the selected template's file contract."""
    expected = artifact_type.strip().lower()
    service.validate()
    configured = service.artifact_type.strip().lower()
    if configured != expected:
        raise DeploymentValidationError(
            f"服务画像制品类型为 {configured or '未配置'}，不能使用 {expected.upper()} 部署模板"
        )
    if not service.artifact_path.lower().endswith(f".{expected}"):
        raise DeploymentValidationError(
            f"目标制品路径必须是 .{expected} 文件"
        )
    artifact.validate(expected_type=expected)


@dataclass(frozen=True, slots=True)
class PlanStep:
    id: str
    name: str
    phase: StepPhase
    action: str
    risk_level: RiskLevel
    mutates_live: bool = False
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _freeze_mapping(self.arguments))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "phase": self.phase.value,
            "action": self.action,
            "risk_level": self.risk_level.value,
            "mutates_live": self.mutates_live,
            "arguments": dict(self.arguments),
        }


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    runbook_id: str
    runbook_version: str
    run_id: str
    service: ServiceProfileSnapshot
    artifact: ArtifactSnapshot
    steps: tuple[PlanStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "runbook_id": self.runbook_id,
            "runbook_version": self.runbook_version,
            "run_id": self.run_id,
            "service": self.service.to_dict(),
            "artifact": self.artifact.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def plan_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def steps_for(self, phase: StepPhase) -> tuple[PlanStep, ...]:
        return tuple(step for step in self.steps if step.phase == phase)


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    run_id: str
    session_id: str
    service_id: str
    target: str
    step: PlanStep
    runbook_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    success: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _freeze_mapping(self.details))


def build_single_java_jar_plan(
    *,
    run_id: str,
    service: ServiceProfileSnapshot,
    artifact: ArtifactSnapshot,
) -> DeploymentPlan:
    """Build the immutable v1 plan for a single-host Java JAR deployment."""
    _validate_template_inputs(service, artifact, artifact_type="jar")
    staging_path = (
        f"{service.deploy_dir.rstrip('/')}/.shell-agent/staging/"
        f"{run_id}/{artifact.name}"
    )
    artifact_name = service.artifact_path.rstrip("/").rsplit("/", 1)[-1]
    backup_path = (
        f"{service.backup_dir.rstrip('/')}/{run_id}/{artifact_name}"
    )
    common = {
        "target": service.target,
        "deploy_dir": service.deploy_dir,
        "artifact_path": service.artifact_path,
    }
    steps: Sequence[PlanStep] = (
        PlanStep(
            "precheck_host",
            "检查目标服务器连通性",
            StepPhase.PRECHECK,
            "precheck_host",
            RiskLevel.SAFE,
            arguments=common,
        ),
        PlanStep(
            "precheck_layout",
            "检查部署目录和启停脚本",
            StepPhase.PRECHECK,
            "precheck_layout",
            RiskLevel.SAFE,
            arguments={**common, "start_cmd": service.start_cmd, "stop_cmd": service.stop_cmd},
        ),
        PlanStep(
            "precheck_disk",
            "检查磁盘空间",
            StepPhase.PRECHECK,
            "precheck_disk",
            RiskLevel.SAFE,
            arguments={**common, "artifact_size": artifact.size},
        ),
        PlanStep(
            "precheck_baseline",
            "记录部署前服务状态",
            StepPhase.PRECHECK,
            "precheck_baseline",
            RiskLevel.SAFE,
            arguments={**common, "status_cmd": service.status_cmd},
        ),
        PlanStep(
            "stage_upload",
            "上传制品到隔离暂存目录",
            StepPhase.EXECUTE,
            "stage_upload",
            RiskLevel.CAUTION,
            arguments={
                **common,
                "local_path": artifact.local_path,
                "staging_path": staging_path,
                "size": artifact.size,
                "sha256": artifact.sha256.lower(),
            },
        ),
        PlanStep(
            "verify_staged_artifact",
            "校验暂存制品",
            StepPhase.EXECUTE,
            "verify_staged_artifact",
            RiskLevel.SAFE,
            arguments={
                **common,
                "staging_path": staging_path,
                "size": artifact.size,
                "sha256": artifact.sha256.lower(),
            },
        ),
        PlanStep(
            "backup_current",
            "备份当前制品",
            StepPhase.EXECUTE,
            "backup_current",
            RiskLevel.CAUTION,
            arguments={**common, "backup_path": backup_path},
        ),
        PlanStep(
            "stop_service",
            "停止服务",
            StepPhase.EXECUTE,
            "stop_service",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "command": service.stop_cmd},
        ),
        PlanStep(
            "switch_artifact",
            "原子替换服务制品",
            StepPhase.EXECUTE,
            "switch_artifact",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "staging_path": staging_path},
        ),
        PlanStep(
            "start_service",
            "启动服务",
            StepPhase.EXECUTE,
            "start_service",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={
                **common,
                "command": service.start_cmd,
                "timeout_seconds": service.startup_timeout_seconds,
            },
        ),
        PlanStep(
            "postcheck_status",
            "检查服务运行状态",
            StepPhase.POSTCHECK,
            "postcheck_status",
            RiskLevel.SAFE,
            arguments={**common, "status_cmd": service.status_cmd},
        ),
        PlanStep(
            "postcheck_health",
            "检查服务健康状态",
            StepPhase.POSTCHECK,
            "postcheck_health",
            RiskLevel.SAFE,
            arguments={
                **common,
                "health_url": service.health_url,
                "ports": list(service.ports),
                "timeout_seconds": service.startup_timeout_seconds,
            },
        ),
        PlanStep(
            "postcheck_artifact",
            "确认远端制品版本",
            StepPhase.POSTCHECK,
            "postcheck_artifact",
            RiskLevel.SAFE,
            arguments={
                **common,
                "size": artifact.size,
                "sha256": artifact.sha256.lower(),
            },
        ),
        PlanStep(
            "rollback_stop",
            "停止当前异常版本",
            StepPhase.ROLLBACK,
            "rollback_stop",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "command": service.stop_cmd},
        ),
        PlanStep(
            "rollback_restore",
            "恢复部署前制品",
            StepPhase.ROLLBACK,
            "rollback_restore",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "backup_path": backup_path},
        ),
        PlanStep(
            "rollback_start",
            "启动回滚版本",
            StepPhase.ROLLBACK,
            "rollback_start",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "command": service.start_cmd},
        ),
        PlanStep(
            "rollback_postcheck_status",
            "检查回滚后服务状态",
            StepPhase.ROLLBACK_POSTCHECK,
            "postcheck_status",
            RiskLevel.SAFE,
            arguments={**common, "status_cmd": service.status_cmd},
        ),
        PlanStep(
            "rollback_postcheck_health",
            "检查回滚后服务健康状态",
            StepPhase.ROLLBACK_POSTCHECK,
            "postcheck_health",
            RiskLevel.SAFE,
            arguments={
                **common,
                "health_url": service.health_url,
                "ports": list(service.ports),
                "timeout_seconds": service.startup_timeout_seconds,
            },
        ),
    )
    return DeploymentPlan(
        runbook_id="single_java_jar_deploy",
        runbook_version="1.0.0",
        run_id=run_id,
        service=service,
        artifact=artifact,
        steps=tuple(steps),
    )


def build_single_tomcat_war_plan(
    *,
    run_id: str,
    service: ServiceProfileSnapshot,
    artifact: ArtifactSnapshot,
) -> DeploymentPlan:
    """Build the immutable v1 plan for one WAR deployed by one Tomcat service.

    Tomcat's exploded context is moved into the deployment backup rather than
    deleted.  This keeps the change reversible and avoids treating a recursive
    delete as an implicit part of a confirmed deployment.
    """
    _validate_template_inputs(service, artifact, artifact_type="war")
    runtime = service.runtime.strip().lower()
    if runtime and "tomcat" not in runtime:
        raise DeploymentValidationError("WAR 部署模板要求服务运行方式为 tomcat")

    deploy_path = PurePosixPath(service.deploy_dir)
    artifact_path = PurePosixPath(service.artifact_path)
    if not artifact_path.is_relative_to(deploy_path):
        raise DeploymentValidationError("WAR 目标路径必须位于 Tomcat 部署目录内")
    context_name = artifact_path.stem
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", context_name):
        raise DeploymentValidationError("WAR Context 名称不安全")

    webapps_dir = str(artifact_path.parent)
    context_path = str(artifact_path.parent / context_name)
    staging_path = (
        f"{service.deploy_dir.rstrip('/')}/.shell-agent/staging/"
        f"{run_id}/{artifact.name}"
    )
    artifact_name = artifact_path.name
    backup_root = f"{service.backup_dir.rstrip('/')}/{run_id}"
    backup_path = f"{backup_root}/{artifact_name}"
    backup_context_path = f"{backup_root}/{context_name}.exploded"
    failed_context_path = (
        f"{service.deploy_dir.rstrip('/')}/.shell-agent/failed/"
        f"{run_id}/{context_name}.exploded"
    )
    common = {
        "target": service.target,
        "deploy_dir": service.deploy_dir,
        "artifact_path": service.artifact_path,
        "webapps_dir": webapps_dir,
        "context_path": context_path,
    }
    steps: Sequence[PlanStep] = (
        PlanStep(
            "precheck_host",
            "检查目标服务器连通性",
            StepPhase.PRECHECK,
            "precheck_host",
            RiskLevel.SAFE,
            arguments=common,
        ),
        PlanStep(
            "precheck_tomcat_layout",
            "检查 Tomcat 目录、WAR 和启停脚本",
            StepPhase.PRECHECK,
            "precheck_tomcat_layout",
            RiskLevel.SAFE,
            arguments={
                **common,
                "start_cmd": service.start_cmd,
                "stop_cmd": service.stop_cmd,
            },
        ),
        PlanStep(
            "precheck_disk",
            "检查磁盘空间",
            StepPhase.PRECHECK,
            "precheck_disk",
            RiskLevel.SAFE,
            arguments={**common, "artifact_size": artifact.size},
        ),
        PlanStep(
            "precheck_baseline",
            "记录部署前 Tomcat 服务状态",
            StepPhase.PRECHECK,
            "precheck_baseline",
            RiskLevel.SAFE,
            arguments={**common, "status_cmd": service.status_cmd},
        ),
        PlanStep(
            "stage_upload",
            "上传 WAR 到隔离暂存目录",
            StepPhase.EXECUTE,
            "stage_upload",
            RiskLevel.CAUTION,
            arguments={
                **common,
                "local_path": artifact.local_path,
                "staging_path": staging_path,
                "size": artifact.size,
                "sha256": artifact.sha256.lower(),
            },
        ),
        PlanStep(
            "verify_staged_artifact",
            "校验暂存 WAR",
            StepPhase.EXECUTE,
            "verify_staged_artifact",
            RiskLevel.SAFE,
            arguments={
                **common,
                "staging_path": staging_path,
                "size": artifact.size,
                "sha256": artifact.sha256.lower(),
            },
        ),
        PlanStep(
            "backup_current",
            "备份当前 WAR",
            StepPhase.EXECUTE,
            "backup_current",
            RiskLevel.CAUTION,
            arguments={**common, "backup_path": backup_path},
        ),
        PlanStep(
            "stop_service",
            "停止 Tomcat 服务",
            StepPhase.EXECUTE,
            "stop_service",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "command": service.stop_cmd},
        ),
        PlanStep(
            "archive_exploded_context",
            "归档当前 Tomcat 解压目录",
            StepPhase.EXECUTE,
            "archive_exploded_context",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={
                **common,
                "backup_context_path": backup_context_path,
            },
        ),
        PlanStep(
            "switch_artifact",
            "原子替换 WAR",
            StepPhase.EXECUTE,
            "switch_artifact",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "staging_path": staging_path},
        ),
        PlanStep(
            "start_service",
            "启动 Tomcat 服务",
            StepPhase.EXECUTE,
            "start_service",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={
                **common,
                "command": service.start_cmd,
                "timeout_seconds": service.startup_timeout_seconds,
            },
        ),
        PlanStep(
            "postcheck_status",
            "检查 Tomcat 运行状态",
            StepPhase.POSTCHECK,
            "postcheck_status",
            RiskLevel.SAFE,
            arguments={**common, "status_cmd": service.status_cmd},
        ),
        PlanStep(
            "postcheck_health",
            "检查 Web 应用健康状态",
            StepPhase.POSTCHECK,
            "postcheck_health",
            RiskLevel.SAFE,
            arguments={
                **common,
                "health_url": service.health_url,
                "ports": list(service.ports),
                "timeout_seconds": service.startup_timeout_seconds,
            },
        ),
        PlanStep(
            "postcheck_artifact",
            "确认远端 WAR 版本",
            StepPhase.POSTCHECK,
            "postcheck_artifact",
            RiskLevel.SAFE,
            arguments={
                **common,
                "size": artifact.size,
                "sha256": artifact.sha256.lower(),
            },
        ),
        PlanStep(
            "rollback_stop",
            "停止异常 Tomcat 版本",
            StepPhase.ROLLBACK,
            "rollback_stop",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "command": service.stop_cmd},
        ),
        PlanStep(
            "rollback_archive_failed_context",
            "隔离失败版本解压目录",
            StepPhase.ROLLBACK,
            "rollback_archive_failed_context",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={
                **common,
                "failed_context_path": failed_context_path,
            },
        ),
        PlanStep(
            "rollback_restore",
            "恢复部署前 WAR",
            StepPhase.ROLLBACK,
            "rollback_restore",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "backup_path": backup_path},
        ),
        PlanStep(
            "rollback_restore_exploded_context",
            "恢复部署前解压目录",
            StepPhase.ROLLBACK,
            "rollback_restore_exploded_context",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={
                **common,
                "backup_context_path": backup_context_path,
            },
        ),
        PlanStep(
            "rollback_start",
            "启动回滚后的 Tomcat",
            StepPhase.ROLLBACK,
            "rollback_start",
            RiskLevel.DANGEROUS,
            mutates_live=True,
            arguments={**common, "command": service.start_cmd},
        ),
        PlanStep(
            "rollback_postcheck_status",
            "检查回滚后 Tomcat 状态",
            StepPhase.ROLLBACK_POSTCHECK,
            "postcheck_status",
            RiskLevel.SAFE,
            arguments={**common, "status_cmd": service.status_cmd},
        ),
        PlanStep(
            "rollback_postcheck_health",
            "检查回滚后 Web 应用健康状态",
            StepPhase.ROLLBACK_POSTCHECK,
            "postcheck_health",
            RiskLevel.SAFE,
            arguments={
                **common,
                "health_url": service.health_url,
                "ports": list(service.ports),
                "timeout_seconds": service.startup_timeout_seconds,
            },
        ),
    )
    return DeploymentPlan(
        runbook_id="single_tomcat_war_deploy",
        runbook_version="1.0.0",
        run_id=run_id,
        service=service,
        artifact=artifact,
        steps=tuple(steps),
    )
