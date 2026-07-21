from __future__ import annotations

import asyncio
from pathlib import Path
import time

from fastapi.testclient import TestClient
import yaml

from shell_agent.runbooks import (
    ExecutionResult,
    RunbookStorage,
    SingleJavaJarDeploymentRuntime,
)
from shell_agent.web.app import create_app
from shell_agent.web.runtime import Runtime


class _AlwaysSuccessfulDeploymentExecutor:
    async def execute(self, request):
        await asyncio.sleep(0)
        return ExecutionResult(
            success=True,
            exit_code=0,
            stdout=f"ok:{request.step.action}",
            details={"action": request.step.action},
        )


def _runtime_config(root: Path) -> Path:
    config = root / "config"
    data = root / "data"
    config.mkdir()
    data.mkdir()
    path = config / "agent.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "llm": {"api_key": "test", "model": "test"},
                "storage": {"sqlite_path": str(data / "shell_agent.db")},
                "ssh": {"default_timeout": 1},
            }
        ),
        encoding="utf-8",
    )
    (config / "credentials.yaml").write_text("credentials: []\n", encoding="utf-8")
    (config / "inventory.yaml").write_text(
        yaml.safe_dump(
            {
                "servers": [
                    {
                        "alias": "dev-01",
                        "host": "127.0.0.1",
                        "port": 22,
                        "env": "dev",
                        "ssh_credential": "unused",
                    }
                ],
                "services": [
                    {
                        "id": "bedcare-mock",
                        "name": "bedcare-mock",
                        "env": "dev",
                        "servers": ["dev-01"],
                        "deploy_dir": "/data/app/bedcare-mock-0.1.0",
                        "artifact_path": "/data/app/bedcare-mock-0.1.0/lib/bedcare-mock.jar",
                        "backup_dir": "/data/backup/bedcare-mock",
                        "artifact_type": "jar",
                        "startup_timeout_seconds": 30,
                        "health_url": "http://127.0.0.1:8091/health",
                        "ports": [8091],
                        "start_cmd": "/data/app/bedcare-mock-0.1.0/bin/start.sh",
                        "stop_cmd": "/data/app/bedcare-mock-0.1.0/bin/stop.sh",
                        "status_cmd": "/data/app/bedcare-mock-0.1.0/bin/status.sh",
                        "verification_status": "verified",
                        "revision": 3,
                    },
                    {
                        "id": "avatar-platform",
                        "name": "avatar-iot-platform",
                        "env": "dev",
                        "servers": ["dev-01"],
                        "deploy_dir": "/opt/apache-tomcat-avatar",
                        "artifact_path": "/opt/apache-tomcat-avatar/webapps/avatar-iot-platform.war",
                        "backup_dir": "/data/backup/avatar-platform",
                        "artifact_type": "war",
                        "runtime": "tomcat",
                        "startup_timeout_seconds": 60,
                        "health_url": "http://127.0.0.1:8080/avatar/health",
                        "ports": [8080],
                        "start_cmd": "/opt/apache-tomcat-avatar/bin/startup.sh",
                        "stop_cmd": "/opt/apache-tomcat-avatar/bin/shutdown.sh",
                        "status_cmd": "/opt/apache-tomcat-avatar/bin/status.sh",
                        "verification_status": "verified",
                        "revision": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_deployment_api_is_session_scoped_idempotent_and_requires_plan_hash(
    tmp_path, monkeypatch
) -> None:
    config_path = _runtime_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("deployment API contract test must never open SSH")

    async def fake_initialize(self: Runtime):
        if getattr(self, "deployment_runtime", None) is None:
            storage = RunbookStorage(self.db)
            await storage.initialize()
            self.deployment_runtime = SingleJavaJarDeploymentRuntime(
                storage, _AlwaysSuccessfulDeploymentExecutor()
            )
        return self.deployment_runtime

    monkeypatch.setattr("shell_agent.executors.ssh.asyncssh.connect", forbidden_connect)
    monkeypatch.setattr(
        Runtime, "initialize_deployment_runtime", fake_initialize, raising=False
    )

    with TestClient(create_app(str(config_path))) as client:
        first_session = client.post(
            "/api/sessions", json={"type": "chat", "title": "deploy"}
        ).json()["session"]
        second_session = client.post(
            "/api/sessions", json={"type": "chat", "title": "other"}
        ).json()["session"]
        first_file = client.post(
            f"/api/sessions/{first_session['id']}/files",
            files={"files": ("app.jar", b"fake-jar-v1", "application/java-archive")},
        ).json()["files"][0]
        second_file = client.post(
            f"/api/sessions/{first_session['id']}/files",
            files={"files": ("app2.jar", b"fake-jar-v2", "application/java-archive")},
        ).json()["files"][0]
        create_body = {
            "session_id": first_session["id"],
            "service_id": "bedcare-mock",
            "file_id": first_file["id"],
            "request_id": "deploy-request-1",
            # This must not auto-confirm a deployment plan.
            "confirm_mode": "full_access",
        }

        cross_session = client.post(
            "/api/deployment-runs",
            json={**create_body, "session_id": second_session["id"]},
        )
        assert cross_session.status_code == 404

        created = client.post("/api/deployment-runs", json=create_body)
        duplicate = client.post("/api/deployment-runs", json=create_body)
        assert created.status_code == 201
        assert duplicate.status_code == 201
        run = created.json()
        assert run["id"] == duplicate.json()["id"]
        assert run["status"] == "waiting_plan_confirm"
        assert run["confirmed_plan_hash"] is None
        assert len(run["steps"]) > 4

        serialized = str(run)
        assert "local_path" not in serialized
        assert "stored_path" not in serialized
        assert str(tmp_path) not in serialized

        reused_for_other_file = client.post(
            "/api/deployment-runs",
            json={**create_body, "file_id": second_file["id"]},
        )
        assert reused_for_other_file.status_code == 409

        listed = client.get(
            "/api/deployment-runs", params={"session_id": first_session["id"]}
        )
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["runs"]] == [run["id"]]
        assert str(tmp_path) not in str(listed.json())

        bad_hash = client.post(
            f"/api/deployment-runs/{run['id']}/confirm",
            json={"plan_hash": "0" * 64},
        )
        assert bad_hash.status_code == 409

        confirmed = client.post(
            f"/api/deployment-runs/{run['id']}/confirm",
            json={"plan_hash": run["plan_hash"]},
        )
        assert confirmed.status_code == 202
        assert confirmed.json()["confirmed_plan_hash"] == run["plan_hash"]

        # A double click must lose the CAS instead of issuing execution twice.
        duplicate_confirm = client.post(
            f"/api/deployment-runs/{run['id']}/confirm",
            json={"plan_hash": run["plan_hash"]},
        )
        assert duplicate_confirm.status_code == 409

        restored = confirmed.json()
        for _ in range(100):
            restored = client.get(f"/api/deployment-runs/{run['id']}").json()
            if restored["status"] == "completed":
                break
            time.sleep(0.01)
        assert restored["status"] == "completed"
        assert all(
            step["status"] == "success"
            for step in restored["steps"]
            if step["phase"] in {"precheck", "execute", "postcheck"}
        )
        assert str(tmp_path) not in str(restored)

        war_file = client.post(
            f"/api/sessions/{first_session['id']}/files",
            files={
                "files": (
                    "avatar-iot-platform.war",
                    b"fake-war-v1",
                    "application/java-archive",
                )
            },
        ).json()["files"][0]
        war_created = client.post(
            "/api/deployment-runs",
            json={
                "session_id": first_session["id"],
                "service_id": "avatar-platform",
                "file_id": war_file["id"],
                "request_id": "deploy-war-request-1",
            },
        )
        assert war_created.status_code == 201
        war_run = war_created.json()
        assert war_run["runbook_id"] == "single_tomcat_war_deploy"
        assert war_run["plan"]["service"]["artifact_type"] == "war"
        assert "archive_exploded_context" in {
            step["action"] for step in war_run["steps"]
        }


def test_deployment_api_cancel_is_durable(tmp_path, monkeypatch) -> None:
    config_path = _runtime_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def fake_initialize(self: Runtime):
        if getattr(self, "deployment_runtime", None) is None:
            storage = RunbookStorage(self.db)
            await storage.initialize()
            self.deployment_runtime = SingleJavaJarDeploymentRuntime(
                storage, _AlwaysSuccessfulDeploymentExecutor()
            )
        return self.deployment_runtime

    monkeypatch.setattr(
        Runtime, "initialize_deployment_runtime", fake_initialize, raising=False
    )
    with TestClient(create_app(str(config_path))) as client:
        session = client.post(
            "/api/sessions", json={"type": "chat", "title": "cancel"}
        ).json()["session"]
        file = client.post(
            f"/api/sessions/{session['id']}/files",
            files={"files": ("app.jar", b"jar", "application/java-archive")},
        ).json()["files"][0]
        created = client.post(
            "/api/deployment-runs",
            json={
                "session_id": session["id"],
                "service_id": "bedcare-mock",
                "file_id": file["id"],
            },
        ).json()

        canceled = client.post(f"/api/deployment-runs/{created['id']}/cancel")
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"
        assert client.get(f"/api/deployment-runs/{created['id']}").json()[
            "status"
        ] == "canceled"
