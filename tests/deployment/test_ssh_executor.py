from __future__ import annotations

from dataclasses import dataclass, field
import shlex
from types import SimpleNamespace
from typing import Callable

import pytest

from shell_agent.core.models import ExecutionResult as SSHExecutionResult
from shell_agent.executors.ssh import VerifiedUploadResult
from shell_agent.runbooks import (
    ExecutionRequest,
    build_single_java_jar_plan,
    build_single_tomcat_war_plan,
)
from shell_agent.runbooks.ssh_executor import SSHDeploymentExecutor
from shell_agent.safety.classifier import classify_command
from shell_agent.safety.policy import EnvironmentPolicyResult

from .test_runbook_models import (
    SHA256,
    artifact_snapshot,
    service_snapshot,
    tomcat_service_snapshot,
    war_artifact_snapshot,
)


def allow_policy(**_kwargs) -> EnvironmentPolicyResult:
    return EnvironmentPolicyResult()


@dataclass
class FakeSSHExecutor:
    environment: str = "test"
    health_body: str = '{"status":"UP"}'
    artifact_size: int = 18_000_000
    artifact_sha256: str = SHA256
    command_results: dict[str, SSHExecutionResult] = field(default_factory=dict)
    commands: list = field(default_factory=list)
    uploads: list[dict] = field(default_factory=list)
    upload_error: BaseException | None = None

    def resolve_server(self, alias: str):
        return SimpleNamespace(alias=alias, env=self.environment)

    async def execute(self, command, timeout=None):
        self.commands.append(command)
        text = command.actual_command
        for marker, result in self.command_results.items():
            if marker in text:
                return result
        if text == "hostname":
            return ssh_result(stdout="dev-01\n")
        if text.startswith("df -Pk"):
            return ssh_result(
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/vda1 99999999 1 99999998 1% /data\n"
                )
            )
        if text.startswith("stat -c %s"):
            return ssh_result(stdout=f"{self.artifact_size}\n")
        if text.startswith("sha256sum"):
            return ssh_result(stdout=f"{self.artifact_sha256}  remote.jar\n")
        if text.startswith("curl "):
            return ssh_result(stdout=self.health_body)
        if text.startswith("ss -lntH"):
            return ssh_result(stdout="LISTEN 0 128 127.0.0.1:8091 0.0.0.0:*\n")
        if text.endswith("/status.sh"):
            return ssh_result(stdout="[status] running\n")
        return ssh_result()

    async def upload_file_verified(self, **kwargs):
        self.uploads.append(dict(kwargs))
        if self.upload_error:
            raise self.upload_error
        remote_path = f"{kwargs['remote_dir'].rstrip('/')}/{kwargs['remote_name']}"
        return VerifiedUploadResult(
            remote_path=remote_path,
            size=int(kwargs["expected_size"]),
            sha256=str(kwargs["expected_sha256"]),
        )


def ssh_result(
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> SSHExecutionResult:
    return SSHExecutionResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=3,
        timed_out=timed_out,
    )


def fake_runtime(fake: FakeSSHExecutor):
    return SimpleNamespace(
        executor=fake,
        db=None,
        config=SimpleNamespace(ssh=SimpleNamespace(default_timeout=30)),
    )


def request_for(action: str, *, run_id: str = "deprun_test", service=None, artifact=None):
    plan = build_single_java_jar_plan(
        run_id=run_id,
        service=service or service_snapshot(),
        artifact=artifact or artifact_snapshot(),
    )
    step = next(step for step in plan.steps if step.action == action)
    return ExecutionRequest(
        run_id=run_id,
        session_id=plan.artifact.session_id,
        service_id=plan.service.service_id,
        target=plan.service.target,
        step=step,
    )


@pytest.mark.asyncio
async def test_adapter_implements_complete_plan_and_uses_verified_atomic_sftp() -> None:
    fake = FakeSSHExecutor()
    adapter = SSHDeploymentExecutor(
        fake_runtime(fake), policy_evaluator=allow_policy, max_health_attempts=1
    )
    plan = build_single_java_jar_plan(
        run_id="deprun_all",
        service=service_snapshot(),
        artifact=artifact_snapshot(),
    )

    for step in plan.steps:
        request = ExecutionRequest(
            run_id=plan.run_id,
            session_id=plan.artifact.session_id,
            service_id=plan.service.service_id,
            target=plan.service.target,
            step=step,
        )
        result = await adapter.execute(request)
        assert result.success, (step.id, result.stderr)

    assert len(fake.uploads) == 1
    upload = fake.uploads[0]
    assert upload["overwrite"] is False
    assert upload["expected_size"] == 18_000_000
    assert upload["expected_sha256"] == SHA256
    assert upload["operation_id"] == "deprun_all"
    assert upload["remote_dir"].endswith("/.shell-agent/staging/deprun_all")
    assert upload["remote_name"] == "bedcare-mock.jar"


@pytest.mark.asyncio
async def test_adapter_executes_tomcat_war_template_without_recursive_delete() -> None:
    fake = FakeSSHExecutor(artifact_size=143_000_000)
    adapter = SSHDeploymentExecutor(
        fake_runtime(fake), policy_evaluator=allow_policy, max_health_attempts=1
    )
    plan = build_single_tomcat_war_plan(
        run_id="deprun_war_all",
        service=tomcat_service_snapshot(),
        artifact=war_artifact_snapshot(),
    )

    for step in plan.steps:
        request = ExecutionRequest(
            run_id=plan.run_id,
            session_id=plan.artifact.session_id,
            service_id=plan.service.service_id,
            target=plan.service.target,
            step=step,
            runbook_id=plan.runbook_id,
        )
        result = await adapter.execute(request)
        assert result.success, (step.id, result.stderr)

    assert fake.uploads[0]["remote_name"] == "avatar-iot-platform.war"
    rendered = "\n".join(item.actual_command for item in fake.commands)
    assert "rm -rf" not in rendered
    assert "/webapps/platform" in rendered
    assert "/data/backup/avatar-platform/deprun_war_all/platform.exploded" in rendered


@pytest.mark.asyncio
async def test_real_system_shutdown_remains_critical_and_blocked() -> None:
    fake = FakeSSHExecutor()
    adapter = SSHDeploymentExecutor(fake_runtime(fake), policy_evaluator=allow_policy)
    service = service_snapshot(stop_cmd="shutdown -h now")

    result = await adapter.execute(request_for("stop_service", service=service))

    assert not result.success
    assert "critical" in result.stderr
    assert fake.commands == []


@pytest.mark.asyncio
async def test_generated_command_arguments_are_shell_quoted() -> None:
    deploy_dir = "/data/app/name with quote's"
    service = service_snapshot(
        deploy_dir=deploy_dir,
        artifact_path=f"{deploy_dir}/lib/bedcare-mock.jar",
        start_cmd=shlex.quote(f"{deploy_dir}/bin/start.sh"),
        stop_cmd=shlex.quote(f"{deploy_dir}/bin/stop.sh"),
    )
    fake = FakeSSHExecutor()
    adapter = SSHDeploymentExecutor(fake_runtime(fake), policy_evaluator=allow_policy)

    result = await adapter.execute(request_for("precheck_layout", service=service))

    assert result.success
    rendered = "\n".join(item.actual_command for item in fake.commands)
    # A quote inside the path is escaped by shlex.quote and cannot terminate the
    # parameter in the rendered shell command.
    assert "test -d '/data/app/name with quote'\"'\"'s'" in rendered


@pytest.mark.asyncio
async def test_remote_checksum_mismatch_fails_even_when_commands_exit_zero() -> None:
    fake = FakeSSHExecutor(artifact_sha256="b" * 64)
    adapter = SSHDeploymentExecutor(fake_runtime(fake), policy_evaluator=allow_policy)

    result = await adapter.execute(request_for("verify_staged_artifact"))

    assert not result.success
    assert "SHA-256" in result.stderr
    assert result.details["remote_sha256"] == "b" * 64


@pytest.mark.asyncio
async def test_exit_zero_does_not_pass_semantically_unhealthy_response() -> None:
    fake = FakeSSHExecutor(health_body='{"status":"DOWN"}')
    adapter = SSHDeploymentExecutor(
        fake_runtime(fake), policy_evaluator=allow_policy, max_health_attempts=1
    )
    status = await adapter.execute(request_for("postcheck_status", run_id="health_run"))
    assert status.success

    health = await adapter.execute(request_for("postcheck_health", run_id="health_run"))

    assert not health.success
    assert "不健康" in health.stderr


@pytest.mark.asyncio
async def test_status_exit_zero_with_inactive_text_blocks_health() -> None:
    fake = FakeSSHExecutor(
        command_results={"/status.sh": ssh_result(stdout="service is inactive\n")}
    )
    adapter = SSHDeploymentExecutor(fake_runtime(fake), policy_evaluator=allow_policy)

    result = await adapter.execute(request_for("postcheck_status"))

    assert not result.success
    assert "退出码为 0" in result.stderr


@pytest.mark.asyncio
async def test_status_exit_zero_with_not_running_text_is_not_healthy() -> None:
    fake = FakeSSHExecutor(
        command_results={"/status.sh": ssh_result(stdout="service is not running\n")}
    )
    adapter = SSHDeploymentExecutor(fake_runtime(fake), policy_evaluator=allow_policy)

    result = await adapter.execute(request_for("postcheck_status"))

    assert not result.success
    assert "退出码为 0" in result.stderr


@pytest.mark.asyncio
async def test_policy_block_prevents_command_execution_after_classification() -> None:
    fake = FakeSSHExecutor()
    classified: list[str] = []

    def recording_classifier(command: str):
        classified.append(command)
        return classify_command(command)

    def blocked_policy(**_kwargs):
        return EnvironmentPolicyResult(blocked=True, block_reason="测试窗口已关闭")

    adapter = SSHDeploymentExecutor(
        fake_runtime(fake),
        classifier=recording_classifier,
        policy_evaluator=blocked_policy,
    )

    result = await adapter.execute(request_for("precheck_host"))

    assert not result.success
    assert "测试窗口已关闭" in result.stderr
    assert classified == ["hostname"]
    assert fake.commands == []


@pytest.mark.asyncio
async def test_prod_is_hard_blocked_before_ssh_or_sftp() -> None:
    fake = FakeSSHExecutor(environment="prod")
    adapter = SSHDeploymentExecutor(fake_runtime(fake), policy_evaluator=allow_policy)

    command_result = await adapter.execute(request_for("precheck_host"))
    upload_result = await adapter.execute(request_for("stage_upload"))

    assert not command_result.success
    assert not upload_result.success
    assert "生产环境" in command_result.stderr
    assert fake.commands == []
    assert fake.uploads == []


@pytest.mark.asyncio
async def test_upload_failure_never_leaks_local_artifact_path() -> None:
    local_path = "/safe/private/session/file-secret.jar"
    fake = FakeSSHExecutor(
        upload_error=ValueError(f"本地文件不存在: {local_path}")
    )
    artifact = artifact_snapshot(local_path=local_path)
    adapter = SSHDeploymentExecutor(fake_runtime(fake), policy_evaluator=allow_policy)

    result = await adapter.execute(request_for("stage_upload", artifact=artifact))

    assert not result.success
    assert local_path not in result.stderr
    assert "本地会话制品" in result.stderr
    assert all(local_path not in str(value) for value in result.details.values())
