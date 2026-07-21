"""Production SSH/SFTP adapter for deterministic deployment runbooks.

The runbook runtime owns state transitions and confirmation.  This adapter is
the last safety boundary before a remote side effect: it renders only the
fixed actions from :mod:`shell_agent.runbooks.models`, re-classifies every
rendered command, evaluates the current environment policy, and then delegates
to the existing :class:`~shell_agent.executors.ssh.SSHExecutor`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit

from loguru import logger

from shell_agent.core.models import AuditRecord, PendingCommand
from shell_agent.safety.audit import write_audit
from shell_agent.safety.classifier import (
    RiskAssessment,
    RiskLevel as CommandRiskLevel,
    classify_command,
)
from shell_agent.safety.policy import (
    EnvironmentPolicyResult,
    evaluate_environment_policy,
)

from .models import (
    ExecutionRequest,
    ExecutionResult,
    RiskLevel as PlanRiskLevel,
)


class _SSHLike(Protocol):
    def resolve_server(self, alias: str) -> Any: ...

    async def execute(self, command: PendingCommand, timeout: int | None = None) -> Any: ...

    async def upload_file_verified(self, **kwargs: Any) -> Any: ...


Classifier = Callable[[str], RiskAssessment]
PolicyEvaluator = Callable[..., EnvironmentPolicyResult]
Sleep = Callable[[float], Awaitable[None]]


_RISK_ORDER = {
    "safe": 0,
    "caution": 1,
    "dangerous": 2,
    "critical": 3,
}
_SHA256_LINE = re.compile(r"^([0-9a-fA-F]{64})(?:\s|$)")
_UNHEALTHY_TEXT = re.compile(
    r"\b(down|unhealthy|failed|failure|dead|inactive|stopped|not\s+ok|not\s+running|not\s+active|out[_ -]of[_ -]service)\b",
    re.I,
)
_UNHEALTHY_ZH = (
    "未运行", "未启动", "已停止", "不健康", "启动失败", "运行失败", "未找到进程",
)
_EXECUTABLE_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")


@dataclass(frozen=True)
class _CommandOutcome:
    result: ExecutionResult
    risk: RiskAssessment


class SSHDeploymentExecutor:
    """Execute one frozen deployment plan through the Web runtime.

    The production constructor is ``SSHDeploymentExecutor(rt)``.  ``rt`` only
    needs the small Runtime surface used here (``executor``, ``db`` and
    ``config``), which also keeps unit tests completely isolated from SSH.
    """

    def __init__(
        self,
        runtime: Any,
        *,
        classifier: Classifier = classify_command,
        policy_evaluator: PolicyEvaluator = evaluate_environment_policy,
        sleep: Sleep = asyncio.sleep,
        health_poll_interval: float = 2.0,
        max_health_attempts: int = 10,
    ) -> None:
        executor = getattr(runtime, "executor", None)
        if executor is None:
            raise RuntimeError("Web Runtime 尚未初始化 SSH 执行器")
        self.runtime = runtime
        self.ssh: _SSHLike = executor
        self.classifier = classifier
        self.policy_evaluator = policy_evaluator
        self.sleep = sleep
        self.health_poll_interval = max(0.0, float(health_poll_interval))
        self.max_health_attempts = max(1, int(max_health_attempts))
        self.default_timeout = self._runtime_default_timeout(runtime)
        # A health action is valid only after this adapter observed a successful
        # semantic status check for the same run.  The durable runtime executes
        # those actions consecutively; after a process restart the run is
        # reconciled to UNKNOWN instead of blindly resuming here.
        self._status_verified_runs: set[str] = set()

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        local_path = str(request.step.arguments.get("local_path") or "")
        try:
            self._validate_target(request)
            environment = self._target_environment(request.target)
            if environment not in {"dev", "test", "prod"}:
                return self._failure("目标服务器环境未配置，禁止执行部署")
            if environment == "prod":
                return self._failure("生产环境禁止使用当前部署 Runbook")

            handler = getattr(self, f"_action_{request.step.action}", None)
            if handler is None:
                return self._failure(f"不支持的部署动作: {request.step.action}")
            result = await handler(request, environment)
            return self._redact_result(result, local_path)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            logger.exception("部署执行器动作失败: {}", request.step.action)
            return self._failure(self._safe_error(exc, local_path))

    async def _action_precheck_host(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        return (await self._run(request, environment, "hostname")).result

    async def _action_precheck_layout(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        checks = [
            ("部署目录不存在", f"test -d {self._q(args['deploy_dir'])}", False),
            ("当前制品不存在", f"test -f {self._q(args['artifact_path'])}", False),
        ]
        for label, configured in (
            ("启动命令不可执行", str(args.get("start_cmd") or "")),
            ("停止命令不可执行", str(args.get("stop_cmd") or "")),
        ):
            checks.append((label, self._executable_check(configured), True))
        for label, command, structured_read_only in checks:
            outcome = await self._run(
                request,
                environment,
                command,
                assessment=(
                    self._structured_readonly_assessment()
                    if structured_read_only else None
                ),
            )
            if not outcome.result.success:
                return self._failure(
                    label,
                    exit_code=outcome.result.exit_code,
                    details=outcome.result.details,
                )
        return self._success(details={"checks": len(checks)})

    async def _action_precheck_tomcat_layout(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        checks = [
            ("Tomcat 部署目录不存在", f"test -d {self._q(args['deploy_dir'])}", False),
            ("Tomcat webapps 目录不存在", f"test -d {self._q(args['webapps_dir'])}", False),
            ("当前 WAR 不存在", f"test -f {self._q(args['artifact_path'])}", False),
        ]
        for label, configured in (
            ("Tomcat 启动命令不可执行", str(args.get("start_cmd") or "")),
            ("Tomcat 停止命令不可执行", str(args.get("stop_cmd") or "")),
        ):
            checks.append((label, self._executable_check(configured), True))
        for label, command, structured_read_only in checks:
            outcome = await self._run(
                request,
                environment,
                command,
                assessment=(
                    self._structured_readonly_assessment()
                    if structured_read_only else None
                ),
            )
            if not outcome.result.success:
                return self._failure(
                    label,
                    exit_code=outcome.result.exit_code,
                    details=outcome.result.details,
                )
        return self._success(details={"checks": len(checks), "runtime": "tomcat"})

    async def _action_precheck_disk(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        outcome = await self._run(
            request,
            environment,
            f"df -Pk -- {self._q(args['deploy_dir'])}",
        )
        if not outcome.result.success:
            return outcome.result
        available = self._parse_df_available(outcome.result.stdout)
        artifact_size = int(args.get("artifact_size") or 0)
        required = artifact_size * 2
        if available < required:
            return self._failure(
                "目标磁盘空间不足，无法同时保存暂存制品和备份",
                details={"available_bytes": available, "required_bytes": required},
            )
        return self._success(
            stdout=outcome.result.stdout,
            details={"available_bytes": available, "required_bytes": required},
        )

    async def _action_precheck_baseline(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        command = str(request.step.arguments.get("status_cmd") or "")
        outcome = await self._run(request, environment, command)
        if not outcome.result.success:
            return outcome.result
        if self._status_is_unhealthy(outcome.result.stdout, outcome.result.stderr):
            return self._failure("部署前服务状态异常，禁止继续部署")
        return outcome.result

    async def _action_stage_upload(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        policy_error = await self._check_non_command_policy(
            request, environment, executor="sftp"
        )
        if policy_error:
            return policy_error
        args = request.step.arguments
        local_path = str(args.get("local_path") or "")
        staging_path = self._remote_path(args.get("staging_path"), "暂存制品路径")
        try:
            uploaded = await self.ssh.upload_file_verified(
                target=request.target,
                local_path=local_path,
                remote_dir=posixpath.dirname(staging_path),
                remote_name=posixpath.basename(staging_path),
                overwrite=False,
                expected_size=int(args.get("size") or 0),
                expected_sha256=str(args.get("sha256") or ""),
                operation_id=request.run_id,
                timeout=max(self.default_timeout, 300),
            )
            if (
                str(uploaded.remote_path) != staging_path
                or int(uploaded.size) != int(args.get("size") or 0)
                or str(uploaded.sha256).lower()
                != str(args.get("sha256") or "").lower()
            ):
                raise IOError("SFTP 返回的远端制品校验信息不一致")
            result = self._success(
                details={
                    "remote_path": staging_path,
                    "size": int(uploaded.size),
                    "sha256": str(uploaded.sha256).lower(),
                    "verified": True,
                    "atomic_publish": True,
                }
            )
            await self._audit_upload(request, environment, result)
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = self._failure(self._safe_error(exc, local_path))
            await self._audit_upload(request, environment, result)
            return result

    async def _action_verify_staged_artifact(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        return await self._verify_remote_artifact(
            request=request,
            environment=environment,
            path=self._remote_path(args.get("staging_path"), "暂存制品路径"),
            expected_size=int(args.get("size") or 0),
            expected_sha256=str(args.get("sha256") or ""),
        )

    async def _action_backup_current(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        source = self._remote_path(args.get("artifact_path"), "目标制品路径")
        backup = self._remote_path(args.get("backup_path"), "备份路径")
        commands = (
            f"install -d -m 0750 -- {self._q(posixpath.dirname(backup))}",
            f"cp -a -- {self._q(source)} {self._q(backup)}",
            f"cmp -s -- {self._q(source)} {self._q(backup)}",
        )
        return await self._run_commands(request, environment, commands)

    async def _action_stop_service(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        command = self._configured_command(request.step.arguments.get("command"))
        return (
            await self._run(
                request,
                environment,
                command,
                assessment=self._service_command_assessment(command),
            )
        ).result

    async def _action_switch_artifact(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        staged = self._remote_path(args.get("staging_path"), "暂存制品路径")
        target = self._remote_path(args.get("artifact_path"), "目标制品路径")
        commands = (
            f"chown --reference={self._q(target)} -- {self._q(staged)}",
            f"chmod --reference={self._q(target)} -- {self._q(staged)}",
            f"mv -fT -- {self._q(staged)} {self._q(target)}",
        )
        return await self._run_commands(request, environment, commands)

    async def _action_archive_exploded_context(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        context = self._remote_path(args.get("context_path"), "Tomcat 解压目录")
        backup = self._remote_path(
            args.get("backup_context_path"), "Tomcat 解压目录备份路径"
        )
        command = (
            f"if test -d {self._q(context)}; then "
            f"install -d -m 0750 -- {self._q(posixpath.dirname(backup))}; "
            f"test ! -e {self._q(backup)}; "
            f"mv -- {self._q(context)} {self._q(backup)}; fi"
        )
        return (await self._run(request, environment, command)).result

    async def _action_start_service(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        return (
            await self._run(
                request,
                environment,
                self._configured_command(request.step.arguments.get("command")),
                timeout=int(
                    request.step.arguments.get("timeout_seconds")
                    or self.default_timeout
                ),
            )
        ).result

    async def _action_postcheck_status(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        command = self._configured_command(
            request.step.arguments.get("status_cmd")
        )
        outcome = await self._run(request, environment, command)
        if not outcome.result.success:
            self._status_verified_runs.discard(request.run_id)
            return outcome.result
        if self._status_is_unhealthy(outcome.result.stdout, outcome.result.stderr):
            self._status_verified_runs.discard(request.run_id)
            return self._failure("状态命令退出码为 0，但输出表明服务未正常运行")
        self._status_verified_runs.add(request.run_id)
        return outcome.result

    async def _action_postcheck_health(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        if request.run_id not in self._status_verified_runs:
            return self._failure("尚未通过服务状态检查，不能判定部署健康")
        args = request.step.arguments
        health_url = str(args.get("health_url") or "").strip()
        ports = tuple(int(port) for port in (args.get("ports") or ()))
        timeout = max(1, int(args.get("timeout_seconds") or self.default_timeout))
        attempts = min(self.max_health_attempts, max(1, timeout // 2))
        last_failure = self._failure("服务健康检查未执行")
        for attempt in range(attempts):
            last_failure = await self._health_attempt(
                request, environment, health_url=health_url, ports=ports, timeout=timeout
            )
            if last_failure.success:
                return last_failure
            if attempt + 1 < attempts and self.health_poll_interval:
                await self.sleep(self.health_poll_interval)
        return last_failure

    async def _action_postcheck_artifact(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        return await self._verify_remote_artifact(
            request=request,
            environment=environment,
            path=self._remote_path(args.get("artifact_path"), "目标制品路径"),
            expected_size=int(args.get("size") or 0),
            expected_sha256=str(args.get("sha256") or ""),
        )

    async def _action_rollback_restore(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        backup = self._remote_path(args.get("backup_path"), "备份路径")
        target = self._remote_path(args.get("artifact_path"), "目标制品路径")
        restore_temp = f"{target}.{request.run_id}.rollback"
        commands = (
            f"cp -a -- {self._q(backup)} {self._q(restore_temp)}",
            f"mv -fT -- {self._q(restore_temp)} {self._q(target)}",
        )
        return await self._run_commands(request, environment, commands)

    async def _action_rollback_archive_failed_context(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        context = self._remote_path(args.get("context_path"), "Tomcat 解压目录")
        failed = self._remote_path(
            args.get("failed_context_path"), "失败版本解压目录"
        )
        command = (
            f"if test -d {self._q(context)}; then "
            f"install -d -m 0750 -- {self._q(posixpath.dirname(failed))}; "
            f"test ! -e {self._q(failed)}; "
            f"mv -- {self._q(context)} {self._q(failed)}; fi"
        )
        return (await self._run(request, environment, command)).result

    async def _action_rollback_restore_exploded_context(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        args = request.step.arguments
        context = self._remote_path(args.get("context_path"), "Tomcat 解压目录")
        backup = self._remote_path(
            args.get("backup_context_path"), "Tomcat 解压目录备份路径"
        )
        command = (
            f"if test -d {self._q(backup)}; then "
            f"test ! -e {self._q(context)}; "
            f"mv -- {self._q(backup)} {self._q(context)}; fi"
        )
        return (await self._run(request, environment, command)).result

    async def _action_rollback_stop(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        return await self._action_stop_service(request, environment)

    async def _action_rollback_start(
        self, request: ExecutionRequest, environment: str
    ) -> ExecutionResult:
        return await self._action_start_service(request, environment)

    async def _health_attempt(
        self,
        request: ExecutionRequest,
        environment: str,
        *,
        health_url: str,
        ports: tuple[int, ...],
        timeout: int,
    ) -> ExecutionResult:
        evidence: dict[str, Any] = {"status_verified": True}
        output: list[str] = []
        if health_url:
            self._validate_health_url(health_url)
            curl_timeout = min(timeout, 15)
            command = (
                "curl --fail --silent --show-error "
                f"--max-time {curl_timeout:d} -- {self._q(health_url)}"
            )
            outcome = await self._run(
                request, environment, command, timeout=curl_timeout + 2
            )
            if not outcome.result.success:
                return self._failure(
                    "健康检查 URL 请求失败",
                    exit_code=outcome.result.exit_code,
                    stderr=outcome.result.stderr,
                )
            if self._health_body_is_unhealthy(outcome.result.stdout):
                return self._failure("健康接口退出码为 0，但返回内容表明服务不健康")
            evidence["health_url"] = health_url
            evidence["health_url_ok"] = True
            output.append(outcome.result.stdout)

        for port in ports:
            if port < 1 or port > 65535:
                return self._failure("健康检查端口超出有效范围")
            filter_value = f"sport = :{port}"
            outcome = await self._run(
                request,
                environment,
                f"ss -lntH {self._q(filter_value)}",
            )
            if not outcome.result.success or not outcome.result.stdout.strip():
                return self._failure(
                    f"服务端口 {port} 未监听",
                    exit_code=outcome.result.exit_code,
                )
            evidence.setdefault("listening_ports", []).append(port)
            output.append(outcome.result.stdout)

        if not health_url and not ports:
            return self._failure("没有配置健康检查 URL 或监听端口")
        return self._success(stdout="\n".join(filter(None, output)), details=evidence)

    async def _verify_remote_artifact(
        self,
        *,
        request: ExecutionRequest,
        environment: str,
        path: str,
        expected_size: int,
        expected_sha256: str,
    ) -> ExecutionResult:
        size_outcome = await self._run(
            request, environment, f"stat -c %s -- {self._q(path)}"
        )
        if not size_outcome.result.success:
            return size_outcome.result
        try:
            remote_size = int(size_outcome.result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return self._failure("无法解析远端制品大小")
        hash_outcome = await self._run(
            request, environment, f"sha256sum -- {self._q(path)}"
        )
        if not hash_outcome.result.success:
            return hash_outcome.result
        match = _SHA256_LINE.match(hash_outcome.result.stdout.strip())
        if not match:
            return self._failure("无法解析远端制品 SHA-256")
        remote_hash = match.group(1).lower()
        if remote_size != expected_size or remote_hash != expected_sha256.lower():
            return self._failure(
                "远端制品大小或 SHA-256 校验失败",
                details={
                    "remote_size": remote_size,
                    "expected_size": expected_size,
                    "remote_sha256": remote_hash,
                    "expected_sha256": expected_sha256.lower(),
                },
            )
        return self._success(
            details={"size": remote_size, "sha256": remote_hash, "verified": True}
        )

    async def _run_commands(
        self,
        request: ExecutionRequest,
        environment: str,
        commands: tuple[str, ...],
    ) -> ExecutionResult:
        output: list[str] = []
        for command in commands:
            outcome = await self._run(request, environment, command)
            if not outcome.result.success:
                return outcome.result
            if outcome.result.stdout:
                output.append(outcome.result.stdout)
        return self._success(stdout="\n".join(output), details={"commands": len(commands)})

    async def _run(
        self,
        request: ExecutionRequest,
        environment: str,
        command: str,
        *,
        timeout: int | None = None,
        assessment: RiskAssessment | None = None,
    ) -> _CommandOutcome:
        configured = self._configured_command(command)
        classified = assessment or self.classifier(configured)
        effective = self._effective_risk(classified, request.step.risk_level)
        policy = self.policy_evaluator(
            env=environment,
            target=request.target,
            executor="ssh",
            risk=effective,
        )
        if environment == "prod":
            result = self._failure("生产环境禁止使用当前部署 Runbook")
            await self._audit_command(request, environment, configured, result, False)
            return _CommandOutcome(result, effective)
        if policy.blocked:
            result = self._failure(
                f"环境策略阻止执行: {policy.block_reason or '未提供原因'}"
            )
            await self._audit_command(request, environment, configured, result, False)
            return _CommandOutcome(result, effective)
        if policy.requires_secondary_confirm:
            result = self._failure("环境策略要求二次确认，当前部署方案不能自动越过")
            await self._audit_command(request, environment, configured, result, False)
            return _CommandOutcome(result, effective)
        if effective.level == CommandRiskLevel.CRITICAL:
            result = self._failure("critical 命令不允许通过部署 Runbook 执行")
            await self._audit_command(request, environment, configured, result, False)
            return _CommandOutcome(result, effective)

        pending = PendingCommand(
            raw=f"ssh {shlex.quote(request.target)} {shlex.quote(configured)}",
            target=request.target,
            target_env=environment,
            executor="ssh",
            actual_command=configured,
            source="runbook",
            intent=request.step.name,
            explanation=f"Deployment Runbook: {request.step.id}",
            display_command=configured,
            skill_name=request.runbook_id or "deployment_runbook",
            step_name=request.step.name,
            task_id=request.run_id,
        )
        try:
            raw = await self.ssh.execute(pending, timeout=timeout or self.default_timeout)
            result = ExecutionResult(
                success=(raw.exit_code == 0 and not bool(raw.timed_out)),
                exit_code=raw.exit_code,
                stdout=str(raw.stdout or ""),
                stderr=str(raw.stderr or ""),
                details={
                    "risk_level": effective.level.value,
                    "risk_reasons": list(effective.reasons),
                    "timed_out": bool(raw.timed_out),
                    "duration_ms": int(raw.duration_ms or 0),
                },
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = self._failure(self._safe_error(exc))
        await self._audit_command(request, environment, configured, result, True)
        return _CommandOutcome(result, effective)

    async def _check_non_command_policy(
        self, request: ExecutionRequest, environment: str, *, executor: str
    ) -> ExecutionResult | None:
        assessment = self._assessment_for_plan_risk(request.step.risk_level)
        policy = self.policy_evaluator(
            env=environment,
            target=request.target,
            executor=executor,
            risk=assessment,
        )
        if environment == "prod":
            return self._failure("生产环境禁止使用当前部署 Runbook")
        if policy.blocked:
            return self._failure(
                f"环境策略阻止执行: {policy.block_reason or '未提供原因'}"
            )
        if policy.requires_secondary_confirm:
            return self._failure("环境策略要求二次确认，当前部署方案不能自动越过")
        if assessment.level == CommandRiskLevel.CRITICAL:
            return self._failure("critical 操作不允许通过部署 Runbook 执行")
        return None

    async def _audit_command(
        self,
        request: ExecutionRequest,
        environment: str,
        command: str,
        result: ExecutionResult,
        executed: bool,
    ) -> None:
        db = getattr(self.runtime, "db", None)
        if db is None:
            return
        record = AuditRecord(
            command=command,
            target=request.target,
            target_env=environment,
            executor="ssh",
            executed=executed,
            source="deployment_runbook",
            caller="web_user",
            session_id=request.session_id,
            user_confirmed=True,
            exit_code=result.exit_code,
            duration_ms=int(result.details.get("duration_ms") or 0),
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=bool(result.details.get("timed_out")),
        )
        try:
            await write_audit(db, record)
        except Exception as exc:  # pragma: no cover - audit cannot hide execution result
            logger.error("部署命令审计写入失败: {}", exc)

    async def _audit_upload(
        self,
        request: ExecutionRequest,
        environment: str,
        result: ExecutionResult,
    ) -> None:
        db = getattr(self.runtime, "db", None)
        if db is None:
            return
        remote = str(request.step.arguments.get("staging_path") or "")
        record = AuditRecord(
            command=f"upload verified artifact -> {remote}",
            target=request.target,
            target_env=environment,
            executor="sftp",
            executed=True,
            source="deployment_runbook",
            caller="web_user",
            session_id=request.session_id,
            user_confirmed=True,
            exit_code=0 if result.success else 1,
            stdout="verified atomic upload" if result.success else "",
            stderr=result.stderr,
        )
        try:
            await write_audit(db, record)
        except Exception as exc:  # pragma: no cover
            logger.error("部署制品审计写入失败: {}", exc)

    def _validate_target(self, request: ExecutionRequest) -> None:
        frozen_target = str(request.step.arguments.get("target") or "")
        if not frozen_target or frozen_target != request.target:
            raise ValueError("执行目标与冻结部署方案不一致")

    def _target_environment(self, target: str) -> str:
        server = self.ssh.resolve_server(target)
        environment = str(getattr(server, "env", "") or "").strip().lower()
        return environment

    @staticmethod
    def _runtime_default_timeout(runtime: Any) -> int:
        config = getattr(runtime, "config", None)
        ssh_config = getattr(config, "ssh", SimpleNamespace(default_timeout=60))
        return max(1, int(getattr(ssh_config, "default_timeout", 60) or 60))

    @staticmethod
    def _q(value: Any) -> str:
        return shlex.quote(str(value))

    @staticmethod
    def _remote_path(value: Any, label: str) -> str:
        raw = str(value or "").strip()
        path = PurePosixPath(raw)
        if not raw.startswith("/") or raw != str(path):
            raise ValueError(f"{label}必须是规范的绝对路径")
        if any(ord(char) < 32 or ord(char) == 127 for char in raw):
            raise ValueError(f"{label}包含非法控制字符")
        return raw

    @staticmethod
    def _configured_command(value: Any) -> str:
        command = str(value or "").strip()
        if not command or any(char in command for char in ("\x00", "\r", "\n")):
            raise ValueError("服务画像中的命令为空或包含非法控制字符")
        return command

    @classmethod
    def _executable_check(cls, configured: str) -> str:
        command = cls._configured_command(configured)
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            raise ValueError("服务画像中的启停命令无法安全解析") from exc
        if not tokens:
            raise ValueError("服务画像中的启停命令为空")
        executable = tokens[0]
        if executable.startswith("/"):
            path = cls._remote_path(executable, "启停命令路径")
            return f"test -x {cls._q(path)}"
        if not _EXECUTABLE_NAME.fullmatch(executable):
            raise ValueError("服务画像中的启停命令入口不安全")
        return f"command -v -- {cls._q(executable)}"

    @staticmethod
    def _parse_df_available(stdout: str) -> int:
        lines = [line.split() for line in stdout.splitlines() if line.strip()]
        for fields in reversed(lines):
            if len(fields) >= 4 and fields[3].isdigit():
                return int(fields[3]) * 1024
        raise ValueError("无法解析远端磁盘可用空间")

    @staticmethod
    def _status_is_unhealthy(stdout: str, stderr: str) -> bool:
        text = f"{stdout}\n{stderr}".strip()
        return bool(_UNHEALTHY_TEXT.search(text)) or any(
            marker in text for marker in _UNHEALTHY_ZH
        )

    @staticmethod
    def _health_body_is_unhealthy(body: str) -> bool:
        text = str(body or "").strip()
        if not text:
            return False  # A successful HTTP 204 is a valid health signal.
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and "status" in payload:
            status = str(payload.get("status") or "").strip().upper()
            return status not in {"UP", "OK", "HEALTHY", "SERVING", "PASS"}
        return bool(_UNHEALTHY_TEXT.search(text)) or any(
            marker in text for marker in _UNHEALTHY_ZH
        )

    @staticmethod
    def _validate_health_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("健康检查 URL 仅允许有效的 HTTP/HTTPS 地址")
        if parsed.username or parsed.password:
            raise ValueError("健康检查 URL 不能包含登录凭证")

    @staticmethod
    def _effective_risk(
        classified: RiskAssessment, declared: PlanRiskLevel
    ) -> RiskAssessment:
        declared_assessment = SSHDeploymentExecutor._assessment_for_plan_risk(declared)
        if _RISK_ORDER[classified.level.value] >= _RISK_ORDER[declared.value]:
            return classified
        return RiskAssessment(
            level=declared_assessment.level,
            reasons=[*classified.reasons, "部署方案声明了更高风险级别"],
            rules=[*classified.rules, "runbook_declared_risk"],
        )

    @staticmethod
    def _assessment_for_plan_risk(risk: PlanRiskLevel) -> RiskAssessment:
        return RiskAssessment(
            level=CommandRiskLevel(risk.value),
            reasons=["部署方案声明的步骤风险"],
            rules=["runbook_declared_risk"],
        )

    @staticmethod
    def _structured_readonly_assessment() -> RiskAssessment:
        return RiskAssessment(
            level=CommandRiskLevel.SAFE,
            reasons=["部署执行器生成的结构化只读文件检查"],
            rules=["runbook_structured_readonly"],
        )

    def _service_command_assessment(self, command: str) -> RiskAssessment:
        """Correct the shutdown.sh filename false positive without weakening shutdown.

        Only a simple invocation of an absolute script named ``shutdown.sh``
        can be downgraded, and it remains dangerous because it stops Tomcat.
        Shell operators or any additional critical rule keep the command at its
        original classification.
        """
        assessed = self.classifier(command)
        if assessed.level != CommandRiskLevel.CRITICAL:
            return assessed
        if set(assessed.rules) != {"shutdown_reboot"}:
            return assessed
        if any(operator in command for operator in (";", "&", "|", "`", "$", "<", ">")):
            return assessed
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return assessed
        if not tokens or not tokens[0].startswith("/"):
            return assessed
        if PurePosixPath(tokens[0]).name.lower() != "shutdown.sh":
            return assessed
        return RiskAssessment(
            level=CommandRiskLevel.DANGEROUS,
            reasons=["已确认 Runbook 调用 Tomcat shutdown.sh 停止服务"],
            rules=["runbook_tomcat_shutdown_script"],
        )

    @staticmethod
    def _safe_error(exc: BaseException, local_path: str = "") -> str:
        message = str(exc).strip() or exc.__class__.__name__
        secrets = {local_path}
        if local_path:
            try:
                secrets.add(str(Path(local_path).resolve(strict=False)))
            except OSError:
                pass
        for secret in sorted((item for item in secrets if item), key=len, reverse=True):
            message = message.replace(secret, "本地会话制品")
        return message[:1000]

    @classmethod
    def _redact_result(cls, result: ExecutionResult, local_path: str) -> ExecutionResult:
        if not local_path:
            return result
        return ExecutionResult(
            success=result.success,
            exit_code=result.exit_code,
            stdout=cls._safe_error(RuntimeError(result.stdout), local_path)
            if result.stdout
            else "",
            stderr=cls._safe_error(RuntimeError(result.stderr), local_path)
            if result.stderr
            else "",
            details=result.details,
        )

    @staticmethod
    def _success(
        *, stdout: str = "", stderr: str = "", details: dict[str, Any] | None = None
    ) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            details=details or {},
        )

    @staticmethod
    def _failure(
        message: str,
        *,
        exit_code: int | None = 1,
        stdout: str = "",
        stderr: str = "",
        details: Any | None = None,
    ) -> ExecutionResult:
        detail_map = dict(details) if isinstance(details, dict) else {}
        return ExecutionResult(
            success=False,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr or message,
            details=detail_map,
        )


__all__ = ["SSHDeploymentExecutor"]
