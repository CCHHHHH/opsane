from __future__ import annotations

import pytest

from shell_agent.runbooks import (
    ArtifactSnapshot,
    DeploymentValidationError,
    RunbookRegistry,
    ServiceProfileSnapshot,
    build_single_java_jar_plan,
    build_single_tomcat_war_plan,
    default_runbook_registry,
)


SHA256 = "a" * 64


def service_snapshot(**overrides) -> ServiceProfileSnapshot:
    values = {
        "service_id": "bedcare-mock",
        "service_name": "Bedcare Mock",
        "revision": 7,
        "verification_status": "verified",
        "environment": "test",
        "target": "dev-01",
        "deploy_dir": "/data/app/bedcare-mock-0.1.0",
        "artifact_path": "/data/app/bedcare-mock-0.1.0/lib/bedcare-mock.jar",
        "backup_dir": "/data/backup/bedcare-mock",
        "start_cmd": "/data/app/bedcare-mock-0.1.0/bin/start.sh",
        "stop_cmd": "/data/app/bedcare-mock-0.1.0/bin/stop.sh",
        "status_cmd": "/data/app/bedcare-mock-0.1.0/bin/status.sh",
        "health_url": "http://127.0.0.1:8091/health",
        "ports": (8091,),
        "startup_timeout_seconds": 45,
    }
    values.update(overrides)
    return ServiceProfileSnapshot(**values)


def artifact_snapshot(**overrides) -> ArtifactSnapshot:
    values = {
        "file_id": "file_001",
        "session_id": "session_001",
        "name": "bedcare-mock.jar",
        "local_path": "/safe/artifacts/bedcare-mock.jar",
        "size": 18_000_000,
        "sha256": SHA256,
    }
    values.update(overrides)
    return ArtifactSnapshot(**values)


def tomcat_service_snapshot(**overrides) -> ServiceProfileSnapshot:
    values = {
        "service_id": "avatar-platform",
        "service_name": "Avatar Platform",
        "revision": 2,
        "verification_status": "verified",
        "environment": "test",
        "target": "dev-01",
        "deploy_dir": "/opt/apache-tomcat-avatar",
        "artifact_path": "/opt/apache-tomcat-avatar/webapps/platform.war",
        "backup_dir": "/data/backup/avatar-platform",
        "start_cmd": "/opt/apache-tomcat-avatar/bin/startup.sh",
        "stop_cmd": "/opt/apache-tomcat-avatar/bin/shutdown.sh",
        "status_cmd": "/opt/apache-tomcat-avatar/bin/status.sh",
        "artifact_type": "war",
        "runtime": "tomcat",
        "health_url": "http://127.0.0.1:8080/platform/health",
        "ports": (8080,),
        "startup_timeout_seconds": 90,
    }
    values.update(overrides)
    return ServiceProfileSnapshot(**values)


def war_artifact_snapshot(**overrides) -> ArtifactSnapshot:
    values = {
        "file_id": "file_war_001",
        "session_id": "session_001",
        "name": "avatar-iot-platform.war",
        "local_path": "/safe/artifacts/avatar-iot-platform.war",
        "size": 143_000_000,
        "sha256": SHA256,
    }
    values.update(overrides)
    return ArtifactSnapshot(**values)


def test_single_java_plan_is_deterministic_and_contains_postcheck_and_rollback() -> None:
    first = build_single_java_jar_plan(
        run_id="deprun_fixed",
        service=service_snapshot(),
        artifact=artifact_snapshot(),
    )
    second = build_single_java_jar_plan(
        run_id="deprun_fixed",
        service=service_snapshot(),
        artifact=artifact_snapshot(),
    )

    assert first.plan_hash == second.plan_hash
    assert len(first.plan_hash) == 64
    assert [step.id for step in first.steps if step.phase.value == "postcheck"] == [
        "postcheck_status",
        "postcheck_health",
        "postcheck_artifact",
    ]
    assert [step.id for step in first.steps if step.phase.value == "rollback"] == [
        "rollback_stop",
        "rollback_restore",
        "rollback_start",
    ]
    assert first.to_dict()["artifact"]["sha256"] == SHA256


@pytest.mark.parametrize(
    ("service", "message"),
    [
        (service_snapshot(environment="prod"), "dev/test"),
        (service_snapshot(verification_status="stale"), "尚未验证"),
        (service_snapshot(health_url="", ports=()), "健康检查"),
        (service_snapshot(artifact_path="/data/app/application.war"), ".jar"),
        (service_snapshot(artifact_path="/data/app/app.jar; reboot"), "绝对路径"),
    ],
)
def test_unsafe_or_incomplete_service_profile_is_rejected(
    service: ServiceProfileSnapshot, message: str
) -> None:
    with pytest.raises(DeploymentValidationError, match=message):
        build_single_java_jar_plan(
            run_id="deprun_invalid",
            service=service,
            artifact=artifact_snapshot(),
        )


def test_non_jar_or_unverifiable_artifact_is_rejected() -> None:
    with pytest.raises(DeploymentValidationError, match="JAR"):
        build_single_java_jar_plan(
            run_id="deprun_bad_artifact",
            service=service_snapshot(),
            artifact=artifact_snapshot(name="payload.zip"),
        )

    with pytest.raises(DeploymentValidationError, match="SHA-256"):
        build_single_java_jar_plan(
            run_id="deprun_bad_hash",
            service=service_snapshot(),
            artifact=artifact_snapshot(sha256="not-a-hash"),
        )


def test_tomcat_war_plan_is_deterministic_and_preserves_exploded_context() -> None:
    first = build_single_tomcat_war_plan(
        run_id="deprun_war_fixed",
        service=tomcat_service_snapshot(),
        artifact=war_artifact_snapshot(),
    )
    second = build_single_tomcat_war_plan(
        run_id="deprun_war_fixed",
        service=tomcat_service_snapshot(),
        artifact=war_artifact_snapshot(),
    )

    assert first.plan_hash == second.plan_hash
    assert first.runbook_id == "single_tomcat_war_deploy"
    assert [step.action for step in first.steps if step.phase.value == "execute"] == [
        "stage_upload",
        "verify_staged_artifact",
        "backup_current",
        "stop_service",
        "archive_exploded_context",
        "switch_artifact",
        "start_service",
    ]
    rollback_actions = [
        step.action for step in first.steps if step.phase.value == "rollback"
    ]
    assert "rollback_archive_failed_context" in rollback_actions
    assert "rollback_restore_exploded_context" in rollback_actions


@pytest.mark.parametrize(
    ("service", "artifact", "message"),
    [
        (tomcat_service_snapshot(runtime="systemd"), war_artifact_snapshot(), "tomcat"),
        (tomcat_service_snapshot(), war_artifact_snapshot(name="platform.jar"), "WAR"),
        (
            tomcat_service_snapshot(artifact_path="/outside/platform.war"),
            war_artifact_snapshot(),
            "Tomcat 部署目录",
        ),
    ],
)
def test_tomcat_war_template_rejects_mismatched_profiles(
    service: ServiceProfileSnapshot,
    artifact: ArtifactSnapshot,
    message: str,
) -> None:
    with pytest.raises(DeploymentValidationError, match=message):
        build_single_tomcat_war_plan(
            run_id="deprun_bad_war", service=service, artifact=artifact
        )


def test_default_registry_selects_deterministic_jar_and_war_templates() -> None:
    registry: RunbookRegistry = default_runbook_registry()

    jar = registry.resolve(service_snapshot(), artifact_snapshot())
    war = registry.resolve(tomcat_service_snapshot(), war_artifact_snapshot())

    assert jar.runbook_id == "single_java_jar_deploy"
    assert war.runbook_id == "single_tomcat_war_deploy"
    assert {item.artifact_type for item in registry.list_templates()} == {"jar", "war"}
