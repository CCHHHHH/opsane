from pathlib import Path
import asyncio
import json
import sqlite3

import pytest
import yaml
from fastapi.testclient import TestClient

from shell_agent.core.models import ExecutionResult, PendingCommand
from shell_agent.web.api import (
    _handle_chat_message,
    _handle_cancel,
    _handle_confirm,
    _handle_completion,
    _handle_direct_command,
    _preview_and_apply_policy,
    _session_pending_state,
)
from shell_agent.web.app import create_app
from shell_agent.web.runtime import get_runtime
from shell_agent.executors.ssh import parse_ssh_command
from shell_agent.storage.database import connect, init_db
from shell_agent.storage.sessions import ensure_session
from shell_agent.storage.tasks import create_task, get_session_tasks, get_task, update_task


def _write_runtime_files(root: Path) -> Path:
    config_dir = root / "config"
    data_dir = root / "data"
    config_dir.mkdir()
    data_dir.mkdir()

    agent_path = config_dir / "agent.yaml"
    agent_path.write_text(
        yaml.safe_dump(
            {
                "llm": {"api_key": "secret-key", "model": "gpt-4o-mini"},
                "storage": {"sqlite_path": str(data_dir / "shell_agent.db")},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "credentials.yaml").write_text("credentials: []\n", encoding="utf-8")
    (config_dir / "inventory.yaml").write_text("servers: []\n", encoding="utf-8")
    return agent_path


def test_config_endpoint_masks_api_key(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with TestClient(create_app(str(agent_path))) as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    llm_config = response.json()["llm"]
    assert llm_config["api_key"] == ""
    assert llm_config["api_key_set"] is True


def test_root_redirects_to_vue_workbench(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path)), follow_redirects=False) as client:
        response = client.get("/")

    assert response.status_code == 307
    assert response.headers["location"] == "/next/#/chat"
    assert response.headers["cache-control"] == "no-store"


def test_modular_state_memory_and_audit_routes_preserve_protocol(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.json() == {
            "current_server": None,
            "stats": {"executed": 0, "failed": 0},
        }

        created = client.post(
            "/api/memories",
            json={
                "subject": "dev-01",
                "predicate": "owner",
                "value": "platform-team",
            },
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["ok"] is True
        memory_id = payload["memory"]["id"]

        memories = client.get("/api/memories?q=platform-team").json()["memories"]
        assert [item["id"] for item in memories] == [memory_id]

        audit = client.get("/api/audit").json()
        assert audit == {"records": []}

        removed = client.delete(f"/api/memories/{memory_id}")
        assert removed.json() == {"ok": True, "error": ""}
        assert client.get("/api/memories").json() == {"memories": []}


def test_config_endpoint_updates_ssh_trust_unknown_hosts(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        response = client.put(
            "/api/config",
            json={
                "section": "ssh",
                "data": {"trust_unknown_hosts": True},
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        config_response = client.get("/api/config")
        assert config_response.json()["ssh"]["trust_unknown_hosts"] is True


def test_services_api_crud_persists_service_profiles(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        server_response = client.post(
            "/api/servers",
            json={
                "alias": "dev-01",
                "host": "10.0.0.12",
                "port": 22,
                "env": "dev",
                "role": "app",
                "ssh_credential": "dev-01-ssh",
                "tags": ["iot"],
            },
        )
        assert server_response.status_code == 200
        assert server_response.json() == {"ok": True}

        create_response = client.post(
            "/api/services",
            json={
                "name": "avatar-iot",
                "env": "dev",
                "servers": ["dev-01"],
                "deploy_dir": "/data/app/apache-tomcat-avatar-iot",
                "artifact_path": "/data/app/apache-tomcat-avatar-iot/lib/avatar-iot.jar",
                "backup_dir": "/data/backup/avatar-iot",
                "artifact_type": "jar",
                "startup_timeout_seconds": 90,
                "log_dir": "/data/app/apache-tomcat-avatar-iot/logs",
                "ports": [8080, 1883],
                "restart_cmd": "bin/restart.sh",
                "tags": ["tomcat"],
            },
        )
        assert create_response.status_code == 200
        payload = create_response.json()
        assert payload["ok"] is True
        assert payload["service"]["id"] == "avatar-iot"

        services = client.get("/api/services").json()["services"]
        assert services[0]["name"] == "avatar-iot"
        assert services[0]["servers"] == ["dev-01"]
        assert services[0]["ports"] == [8080, 1883]
        assert services[0]["artifact_path"].endswith("avatar-iot.jar")
        assert services[0]["backup_dir"] == "/data/backup/avatar-iot"
        assert services[0]["startup_timeout_seconds"] == 90
        assert "服务画像会根据当前问题按需注入" in get_runtime().llm.system_prompt
        assert "avatar-iot" not in get_runtime().llm.system_prompt

        update_response = client.put(
            "/api/services/avatar-iot",
            json={
                "id": "avatar-iot",
                "name": "avatar-iot",
                "env": "dev",
                "servers": ["dev-01"],
                "deploy_dir": "/data/app/avatar-iot",
                "log_dir": "/data/logs/avatar-iot",
                "ports": [8080],
                "status_cmd": "bin/status.sh",
            },
        )
        assert update_response.status_code == 200
        assert update_response.json()["ok"] is True

        saved = yaml.safe_load((tmp_path / "config" / "inventory.yaml").read_text())
        assert saved["services"][0]["log_dir"] == "/data/logs/avatar-iot"

        delete_response = client.delete("/api/services/avatar-iot")
        assert delete_response.status_code == 200
        assert delete_response.json() == {"ok": True}
        assert client.get("/api/services").json()["services"] == []


def test_safety_config_api_reads_defaults_and_saves_audited_config(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        response = client.get("/api/safety/config")
        assert response.status_code == 200
        payload = response.json()
        assert payload["environment_source"] == "default"
        assert payload["environments"]["prod"]["require_secondary_confirm"] is True

        update_response = client.put(
            "/api/safety/config",
            json={
                "environments": {
                    "prod": {
                        "require_secondary_confirm": True,
                        "secondary_confirm_levels": ["critical", "dangerous"],
                        "forbidden_executors": [],
                        "time_window": {"dangerous_allowed": ["10:00-18:00"]},
                    }
                },
                "safe_patterns": ["^\\s*redis-cli\\s+info\\b"],
                "forbidden_patterns": [
                    {
                        "name": "custom_flush",
                        "level": "critical",
                        "pattern": "\\bcustomctl\\s+flush\\b",
                        "reason": "自定义 flush 高风险",
                    }
                ],
            },
        )
        assert update_response.status_code == 200
        assert update_response.json() == {"ok": True}

        saved = yaml.safe_load((tmp_path / "config" / "safety" / "forbidden_patterns.yaml").read_text())
        assert saved["patterns"][0]["name"] == "custom_flush"

        audit_records = client.get("/api/audit?target=safety-config").json()["records"]
        assert audit_records[0]["command"] == "update safety config"
        assert "safe_patterns=1" in audit_records[0]["stdout"]


def test_safety_config_rejects_invalid_regex(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        response = client.put(
            "/api/safety/config",
            json={
                "environments": {},
                "safe_patterns": ["["],
                "forbidden_patterns": [],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "unterminated character set" in payload["error"]


def test_safety_classify_api_returns_policy_result(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        response = client.post(
            "/api/safety/classify",
            json={
                "command": "rm app.log",
                "target": "prod-01",
                "env": "prod",
                "executor": "ssh",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["risk_level"] == "dangerous"
    assert payload["requires_secondary_confirm"] is True
    assert payload["secondary_confirm_expected"] == "prod-01"


def test_skills_api_lists_template_skills(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    with TestClient(create_app(str(agent_path))) as client:
        response = client.get("/api/skills")

    assert response.status_code == 200
    names = {skill["name"] for skill in response.json()["skills"]}
    assert {"resource_summary", "java_processes", "list_directory"} <= names


def test_sessions_api_create_list_get_delete(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        create_response = client.post(
            "/api/sessions",
            json={"type": "chat", "title": "排查磁盘"},
        )
        assert create_response.status_code == 200
        session = create_response.json()["session"]
        assert session["type"] == "chat"
        assert session["title"] == "排查磁盘"

        list_response = client.get("/api/sessions?type=chat")
        sessions = list_response.json()["sessions"]
        assert any(item["id"] == session["id"] for item in sessions)

        detail_response = client.get(f"/api/sessions/{session['id']}")
        detail = detail_response.json()["session"]
        assert detail["id"] == session["id"]
        assert detail["messages"] == []
        assert detail["tasks"] == []

        db_path = tmp_path / "data" / "shell_agent.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_tasks (
                    id, session_id, channel, status, title, current_step,
                    total_steps, pending_command, pending_target, confirm_mode,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "task_api",
                    session["id"],
                    "chat",
                    "running",
                    "恢复任务",
                    1,
                    2,
                    "",
                    "",
                    "auto_safe",
                    "now",
                    "now",
                ),
            )
            conn.execute(
                """
                INSERT INTO agent_task_events (
                    id, task_id, session_id, channel, type, status,
                    step_index, content, payload, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "evt_api",
                    "task_api",
                    session["id"],
                    "chat",
                    "task_step",
                    "running",
                    1,
                    "执行中",
                    json.dumps({"task_id": "task_api", "status": "running"}),
                    "now",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        runtime = get_runtime()
        runtime.running_task_ids[f"{session['id']}:chat"] = "task_api"
        detail_with_task = client.get(f"/api/sessions/{session['id']}").json()["session"]
        assert detail_with_task["tasks"][0]["id"] == "task_api"
        assert detail_with_task["tasks"][0]["events"][0]["type"] == "task_step"

        runtime.running_task_ids.pop(f"{session['id']}:chat")
        read_only_detail = client.get(f"/api/sessions/{session['id']}").json()["session"]
        assert read_only_detail["tasks"][0]["id"] == "task_api"
        conn = sqlite3.connect(db_path)
        try:
            status, completed_at = conn.execute(
                "SELECT status, completed_at FROM agent_tasks WHERE id = ?",
                ("task_api",),
            ).fetchone()
        finally:
            conn.close()
        assert status == "running"
        assert completed_at is None

        rename_response = client.patch(
            f"/api/sessions/{session['id']}",
            json={"title": "dev-01 磁盘排查"},
        )
        renamed = rename_response.json()["session"]
        assert renamed["title"] == "dev-01 磁盘排查"

        search_response = client.get("/api/sessions?type=chat&q=磁盘")
        search_sessions = search_response.json()["sessions"]
        assert any(item["id"] == session["id"] for item in search_sessions)

        delete_response = client.delete(f"/api/sessions/{session['id']}")
        assert delete_response.json() == {"ok": True}

        list_after_delete = client.get("/api/sessions?type=chat").json()["sessions"]
        assert all(item["id"] != session["id"] for item in list_after_delete)


def test_session_detail_can_expand_beyond_500_messages(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        session = client.post(
            "/api/sessions",
            json={"type": "chat", "title": "长会话"},
        ).json()["session"]

        conn = sqlite3.connect(tmp_path / "data" / "shell_agent.db")
        try:
            conn.executemany(
                """
                INSERT INTO session_messages (
                    id, session_id, role, type, content, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"msg_{index:04d}",
                        session["id"],
                        "user",
                        "user_message",
                        f"message-{index}",
                        "{}",
                        f"{index:06d}",
                    )
                    for index in range(520)
                ],
            )
            conn.commit()
        finally:
            conn.close()

        detail = client.get(
            f"/api/sessions/{session['id']}?message_limit=501"
        ).json()["session"]

    assert len(detail["messages"]) == 501
    assert detail["messages_truncated"] == 19
    assert detail["messages"][0]["content"] == "message-19"
    assert detail["messages"][-1]["content"] == "message-519"


def test_sessions_api_pins_and_unpins_session(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        first = client.post(
            "/api/sessions",
            json={"type": "chat", "title": "第一个会话"},
        ).json()["session"]
        second = client.post(
            "/api/sessions",
            json={"type": "chat", "title": "第二个会话"},
        ).json()["session"]

        pinned = client.put(
            f"/api/sessions/{first['id']}/pin",
            json={"pinned": True},
        ).json()["session"]
        sessions = client.get("/api/sessions?type=chat").json()["sessions"]

        assert pinned["pinned_at"]
        assert sessions[0]["id"] == first["id"]
        assert sessions[0]["pinned_at"]
        assert sessions[1]["id"] == second["id"]

        unpinned = client.put(
            f"/api/sessions/{first['id']}/pin",
            json={"pinned": False},
        ).json()["session"]

    assert unpinned["pinned_at"] is None


def test_credentials_api_masks_and_updates_values(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app(str(agent_path))) as client:
        create_response = client.post(
            "/api/credentials",
            json={
                "id": "deploy",
                "type": "password",
                "username": "root",
                "password": "secret",
            },
        )
        assert create_response.status_code == 200
        assert create_response.json() == {"ok": True}

        list_response = client.get("/api/credentials")
        assert list_response.status_code == 200
        credentials = list_response.json()["credentials"]
        assert credentials == [
            {
                "id": "deploy",
                "type": "password",
                "username": "root",
                "password_set": True,
                "private_key_set": False,
                "passphrase_set": False,
            }
        ]
        assert "secret" not in list_response.text

        update_response = client.post(
            "/api/credentials",
            json={
                "id": "deploy",
                "type": "key",
                "username": "deploy",
                "private_key": "~/.ssh/id_rsa",
                "passphrase": "phrase",
            },
        )
        assert update_response.status_code == 200
        assert update_response.json() == {"ok": True}

        list_response = client.get("/api/credentials")
        credentials = list_response.json()["credentials"]
        assert credentials[0]["type"] == "key"
        assert credentials[0]["username"] == "deploy"
        assert credentials[0]["password_set"] is False
        assert credentials[0]["private_key_set"] is True
        assert credentials[0]["passphrase_set"] is True
        assert '"phrase"' not in list_response.text

        delete_response = client.delete("/api/credentials/deploy")
        assert delete_response.status_code == 200
        assert delete_response.json() == {"ok": True}
        assert client.get("/api/credentials").json()["credentials"] == []


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def _message_types(messages: list[dict], *, include_turn_state: bool = False) -> list[str]:
    return [
        message["type"]
        for message in messages
        if include_turn_state or message["type"] != "turn_state"
    ]


def _last_messages_without_turn_state(messages: list[dict], count: int) -> list[dict]:
    return [message for message in messages if message["type"] != "turn_state"][-count:]


def _turn_state_values(messages: list[dict]) -> list[tuple[str, str]]:
    return [
        (message["status"], message["label"])
        for message in messages
        if message["type"] == "turn_state"
    ]


class BrokenWebSocket:
    async def send_json(self, payload: dict) -> None:
        raise RuntimeError("websocket disconnected")


class FakeExecutor:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.normalized_inputs = []

    def normalize(self, raw_command: str) -> PendingCommand:
        self.normalized_inputs.append(raw_command)
        parsed = parse_ssh_command(raw_command)
        if parsed is None:
            raise ValueError(f"无法解析 SSH 命令: {raw_command}")
        target, actual_command = parsed
        return PendingCommand(
            raw=raw_command,
            target=target,
            target_env="test",
            executor="ssh",
            actual_command=actual_command,
        )

    async def execute(self, command: PendingCommand) -> ExecutionResult:
        self.execute_calls += 1
        stdout = "ok"
        if command.cwd_update:
            if "/data/app" in command.actual_command:
                stdout = "/data/app\n"
            else:
                stdout = "/root\n"
        elif command.actual_command == "cd /data/app && pwd":
            stdout = "/data/app\n"
        elif "compgen -f" in command.actual_command:
            stdout = "logs/\napp.log\n" if "cd /data/app" in command.actual_command else "root.log\n"
        return ExecutionResult(
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_ms=3,
        )


class SlowExecutor(FakeExecutor):
    async def execute(self, command: PendingCommand) -> ExecutionResult:
        self.execute_calls += 1
        await asyncio.sleep(10)
        return ExecutionResult(
            exit_code=0,
            stdout="late",
            stderr="",
            duration_ms=10000,
        )


class FakeRuntime:
    def __init__(self) -> None:
        self.executor = FakeExecutor()
        self.pending_commands = {}
        self.running_tasks = {}
        self.session_contexts = {}
        self.audit_records = []
        self.servers = {"unit-host": object()}
        self.llm = None


async def _drain_runtime_tasks(runtime: FakeRuntime) -> None:
    tasks = list(runtime.running_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


class FakeLLM:
    def __init__(self) -> None:
        self.calls = []
        self.analysis_calls = []
        self.next_step_calls = []
        self.command_response = "收到"
        self.analysis_response = "分析：磁盘空间正常"
        self.next_step_response = {"done": True, "summary": "无需继续执行下一步"}

    async def generate_command(self, user_input: str, history=None):
        self.calls.append({"user_input": user_input, "history": history or []})
        return self.command_response

    async def analyze_execution_result(
        self,
        user_input: str,
        command: str,
        output: str,
        exit_code,
        timed_out: bool,
        history=None,
    ):
        self.analysis_calls.append(
            {
                "user_input": user_input,
                "command": command,
                "output": output,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "history": history or [],
            }
        )
        return self.analysis_response

    async def decide_next_step(
        self,
        user_input: str,
        command: str,
        analysis: str,
        step_index: int,
        max_steps: int,
        history=None,
    ):
        self.next_step_calls.append(
            {
                "user_input": user_input,
                "command": command,
                "analysis": analysis,
                "step_index": step_index,
                "max_steps": max_steps,
                "history": history or [],
            }
        )
        return self.next_step_response


@pytest.mark.asyncio
async def test_web_direct_command_waits_for_confirmation(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append(
            {
                "command": command.actual_command,
                "executed": executed,
                **kwargs,
            }
        )

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_test",
        "ssh unit-host 'df -h'",
    )

    assert runtime.executor.execute_calls == 0
    assert "sess_test:command" in runtime.pending_commands
    assert [message["type"] for message in websocket.messages] == [
        "command_preview",
        "confirm_prompt",
    ]
    preview = websocket.messages[0]
    assert preview["confirm_mode"] == "interactive"
    assert preview["risk_level"] == "safe"
    assert preview["risk_reasons"]
    assert preview["risk_rules"] == ["known_read_only"]

    await _handle_confirm(websocket, runtime, "sess_test", confirmed=True, channel="command")
    await _drain_runtime_tasks(runtime)

    assert runtime.executor.execute_calls == 1
    assert "sess_test:command" not in runtime.pending_commands
    assert runtime.audit_records == [
        {
            "command": "df -h",
            "executed": True,
            "user_confirmed": True,
            "session_id": "sess_test",
            "exit_code": 0,
            "duration_ms": 3,
            "stdout": "ok",
            "stderr": "",
            "truncated": False,
            "timed_out": False,
        }
    ]
    assert websocket.messages[-1]["type"] == "execution_result"
    assert websocket.messages[-1]["command"] == "df -h"
    assert websocket.messages[-1]["target"] == "unit-host"
    assert websocket.messages[-1]["timed_out"] is False
    assert websocket.messages[-2]["type"] == "execution_status"
    assert websocket.messages[-2]["status"] == "success"


@pytest.mark.asyncio
async def test_web_prod_dangerous_command_requires_secondary_confirmation(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)
    monkeypatch.setattr("shell_agent.safety.policy.read_safety_yaml", lambda _filename: {})

    command = PendingCommand(
        raw="ssh prod-01 'rm app.log'",
        target="prod-01",
        target_env="prod",
        executor="ssh",
        actual_command="rm app.log",
    )
    await _preview_and_apply_policy(
        websocket=websocket,
        rt=runtime,
        session_id="sess_prod",
        command=command,
        confirm_mode="interactive",
        channel="command",
    )

    preview = websocket.messages[0]
    assert preview["risk_level"] == "dangerous"
    assert preview["requires_secondary_confirm"] is True
    assert preview["secondary_confirm_expected"] == "prod-01"
    assert "sess_prod:command" in runtime.pending_commands

    await _handle_confirm(
        websocket,
        runtime,
        "sess_prod",
        confirmed=True,
        channel="command",
        secondary_confirm_value="wrong",
    )
    await _drain_runtime_tasks(runtime)

    assert runtime.executor.execute_calls == 0
    assert "sess_prod:command" in runtime.pending_commands
    assert websocket.messages[-1]["type"] == "system"
    assert "二次确认不匹配" in websocket.messages[-1]["content"]

    await _handle_confirm(
        websocket,
        runtime,
        "sess_prod",
        confirmed=True,
        channel="command",
        secondary_confirm_value="prod-01",
    )
    await _drain_runtime_tasks(runtime)

    assert runtime.executor.execute_calls == 1
    assert "sess_prod:command" not in runtime.pending_commands
    assert runtime.audit_records[-1]["executed"] is True


@pytest.mark.asyncio
async def test_web_direct_bare_command_uses_selected_target(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_bare",
        "df -h",
        target="unit-host",
        confirm_mode="dry_run",
    )

    assert runtime.executor.normalized_inputs == ["ssh unit-host 'df -h'"]
    assert "sess_bare:command" not in runtime.pending_commands
    assert websocket.messages[1]["channel"] == "command"


@pytest.mark.asyncio
async def test_web_cancel_pending_direct_command(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_cancel_pending",
        "rm app.log",
        target="unit-host",
        confirm_mode="interactive",
    )
    assert "sess_cancel_pending:command" in runtime.pending_commands

    await _handle_cancel(websocket, runtime, "sess_cancel_pending", channel="command")

    assert "sess_cancel_pending:command" not in runtime.pending_commands
    assert runtime.executor.execute_calls == 0
    assert runtime.audit_records[-1]["executed"] is False
    assert websocket.messages[-1]["type"] == "system"
    assert websocket.messages[-1]["content"] == "已取消"


@pytest.mark.asyncio
async def test_web_cancel_running_direct_command(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.executor = SlowExecutor()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_cancel_running",
        "df -h",
        target="unit-host",
        confirm_mode="auto_safe",
    )
    await asyncio.sleep(0)
    assert runtime.executor.execute_calls == 1
    assert "sess_cancel_running:command" in runtime.running_tasks

    await _handle_cancel(websocket, runtime, "sess_cancel_running", channel="command")
    await _drain_runtime_tasks(runtime)

    assert "sess_cancel_running:command" not in runtime.running_tasks
    assert runtime.audit_records[-1]["stderr"] == "执行已取消"
    assert websocket.messages[-2]["type"] == "execution_status"
    assert websocket.messages[-2]["status"] == "canceled"
    assert websocket.messages[-1]["type"] == "execution_result"
    assert websocket.messages[-1]["output"] == "执行已取消"


@pytest.mark.asyncio
async def test_web_direct_command_can_target_another_server(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.servers = {"unit-host": object(), "dev-02": object()}
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_target",
        "df -h",
        target="dev-02",
        confirm_mode="dry_run",
    )

    assert runtime.executor.normalized_inputs == ["ssh dev-02 'df -h'"]
    assert websocket.messages[0]["target"] == "dev-02"
    assert websocket.messages[0]["command"] == "df -h"


@pytest.mark.asyncio
async def test_web_direct_command_preserves_cwd_per_target(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_cwd",
        "cd /data/app",
        target="unit-host",
        confirm_mode="auto_safe",
    )
    await _drain_runtime_tasks(runtime)

    assert runtime.executor.execute_calls == 1
    assert runtime.session_contexts["sess_cwd"].get_cwd("unit-host") == "/data/app"
    assert websocket.messages[0]["command"] == "cd /data/app"
    assert websocket.messages[-1]["command"] == "cd /data/app"
    assert runtime.audit_records[0]["command"] == "cd /data/app && pwd"

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_cwd",
        "pwd",
        target="unit-host",
        confirm_mode="dry_run",
    )

    assert websocket.messages[-2]["type"] == "command_preview"
    assert websocket.messages[-2]["command"] == "pwd"
    assert runtime.audit_records[-1]["command"] == "cd /data/app && pwd"


@pytest.mark.asyncio
async def test_web_completion_returns_command_candidates() -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()

    await _handle_completion(
        websocket,
        runtime,
        "sess_complete",
        "l",
        cursor=1,
        target="unit-host",
        request_id="req-1",
        input_id="command-input",
    )

    message = websocket.messages[-1]
    assert message["type"] == "completion_result"
    assert message["request_id"] == "req-1"
    assert message["input_id"] == "command-input"
    assert message["kind"] == "command"
    assert "ls" in message["candidates"]
    assert "ll" in message["candidates"]


@pytest.mark.asyncio
async def test_web_completion_uses_direct_command_cwd(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_complete_cwd",
        "cd /data/app",
        target="unit-host",
        confirm_mode="auto_safe",
    )
    await _drain_runtime_tasks(runtime)
    await _handle_completion(
        websocket,
        runtime,
        "sess_complete_cwd",
        "ls lo",
        cursor=5,
        target="unit-host",
        request_id="req-2",
        input_id="command-input",
    )

    assert runtime.session_contexts["sess_complete_cwd"].get_cwd("unit-host") == "/data/app"
    assert "cd /data/app && compgen -f" in runtime.executor.normalized_inputs[-1]
    message = websocket.messages[-1]
    assert message["type"] == "completion_result"
    assert message["kind"] == "path"
    assert message["prefix"] == "lo"
    assert "logs/" in message["candidates"]


@pytest.mark.asyncio
async def test_web_auto_safe_executes_safe_command(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append(
            {
                "command": command.actual_command,
                "executed": executed,
                **kwargs,
            }
        )

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_auto_safe",
        "ssh unit-host 'df -h'",
        confirm_mode="auto_safe",
    )
    await _drain_runtime_tasks(runtime)

    assert runtime.executor.execute_calls == 1
    assert "sess_auto_safe" not in runtime.pending_commands
    assert [message["type"] for message in websocket.messages] == [
        "command_preview",
        "system",
        "execution_status",
        "execution_status",
        "execution_result",
    ]
    assert websocket.messages[0]["confirm_mode"] == "auto_safe"
    assert runtime.audit_records[0]["executed"] is True
    assert runtime.audit_records[0]["user_confirmed"] is True


@pytest.mark.asyncio
async def test_web_push_failure_does_not_stop_auto_safe_execution(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = BrokenWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_disconnected",
        "df -h",
        confirm_mode="auto_safe",
        target="unit-host",
    )
    await _drain_runtime_tasks(runtime)

    assert runtime.executor.execute_calls == 1
    assert runtime.audit_records[0]["command"] == "df -h"
    assert runtime.audit_records[0]["executed"] is True


@pytest.mark.asyncio
async def test_web_dry_run_never_executes(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append(
            {
                "command": command.actual_command,
                "executed": executed,
                **kwargs,
            }
        )

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_dry_run",
        "ssh unit-host 'df -h'",
        confirm_mode="dry_run",
    )

    assert runtime.executor.execute_calls == 0
    assert "sess_dry_run" not in runtime.pending_commands
    assert [message["type"] for message in websocket.messages] == [
        "command_preview",
        "system",
    ]
    assert websocket.messages[0]["confirm_mode"] == "dry_run"
    assert runtime.audit_records == [
        {
            "command": "df -h",
            "executed": False,
            "user_confirmed": None,
            "session_id": "sess_dry_run",
        }
    ]


@pytest.mark.asyncio
async def test_web_llm_receives_session_context_from_direct_command(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_context",
        "ssh unit-host 'df -h'",
        confirm_mode="auto_safe",
    )
    await _drain_runtime_tasks(runtime)
    await _handle_chat_message(
        websocket,
        runtime,
        "sess_context",
        "继续看一下",
        target="unit-host",
    )

    assert len(runtime.llm.calls) == 1
    history = runtime.llm.calls[0]["history"]
    assert history
    context_text = history[0]["content"]
    assert "当前选中目标服务器 alias: unit-host" in context_text
    assert "unit-host $ df -h" in context_text
    assert "输出摘要" in context_text
    assert "ok" in context_text


@pytest.mark.asyncio
async def test_web_chat_session_contexts_are_isolated(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_direct_command(
        websocket,
        runtime,
        "sess_a",
        "ssh unit-host 'df -h'",
        confirm_mode="auto_safe",
    )
    await _drain_runtime_tasks(runtime)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_b",
        "继续看一下",
        target="unit-host",
    )

    history = runtime.llm.calls[-1]["history"]
    context_text = history[0]["content"]
    assert "df -h" not in context_text
    assert "输出摘要" not in context_text


@pytest.mark.asyncio
async def test_web_chat_template_skill_bypasses_llm(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_skill",
        "看下 /data/app 目录内容",
        confirm_mode="interactive",
        target="unit-host",
    )

    assert runtime.llm.calls == []
    pending = runtime.pending_commands["sess_skill:chat"]
    assert pending.source == "skill"
    assert pending.skill_name == "list_directory"
    assert pending.actual_command == "ls -la /data/app"
    assert pending.intent == "查看 unit-host 上 /data/app 目录的详细内容"
    assert _message_types(websocket.messages) == [
        "user_message",
        "agent",
        "command_preview",
        "confirm_prompt",
    ]


@pytest.mark.asyncio
async def test_web_chat_blocks_new_message_when_chat_command_pending(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    websocket = FakeWebSocket()
    pending = PendingCommand(
        raw="ssh unit-host 'rm -f /var/log/app.log'",
        target="unit-host",
        target_env="dev",
        executor="ssh",
        actual_command="rm -f /var/log/app.log",
        source="llm",
    )
    runtime.pending_commands["sess_blocked:chat"] = pending

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_blocked",
        "继续帮我看一下",
        confirm_mode="auto_safe",
        target="unit-host",
    )

    assert runtime.llm.calls == []
    assert runtime.pending_commands["sess_blocked:chat"] is pending
    assert _message_types(websocket.messages) == [
        "user_message",
        "system",
    ]
    assert "当前会话还有待确认命令" in websocket.messages[-1]["content"]
    assert "rm -f /var/log/app.log" in websocket.messages[-1]["content"]


@pytest.mark.asyncio
async def test_web_chat_blocks_db_waiting_confirm_task_after_restart(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    runtime = FakeRuntime()
    runtime.db = db
    runtime.llm = FakeLLM()
    websocket = FakeWebSocket()
    try:
        await ensure_session(db, "sess_db_blocked", session_type="chat", title="删除日志")
        task = await create_task(
            db,
            session_id="sess_db_blocked",
            channel="chat",
            title="删除日志",
            confirm_mode="auto_safe",
        )
        await update_task(
            db,
            task["id"],
            status="waiting_confirm",
            pending_command="rm -f /tmp/app.log",
            pending_target="unit-host",
        )

        await _handle_chat_message(
            websocket,
            runtime,
            "sess_db_blocked",
            "继续帮我看一下",
            confirm_mode="auto_safe",
            target="unit-host",
        )

        assert runtime.llm.calls == []
        assert _message_types(websocket.messages) == [
            "user_message",
            "system",
        ]
        assert "当前会话还有待确认命令" in websocket.messages[-1]["content"]
        assert "rm -f /tmp/app.log" in websocket.messages[-1]["content"]
        all_tasks = await get_session_tasks(
            db,
            "sess_db_blocked",
            channel="chat",
            include_completed=True,
        )
        blocked_turn = next(item for item in all_tasks if item["id"] != task["id"])
        assert blocked_turn["status"] == "blocked"
        assert blocked_turn["completed_at"]

        await _handle_confirm(
            websocket,
            runtime,
            "sess_db_blocked",
            confirmed=False,
            channel="chat",
            task_id=task["id"],
        )

        updated = await get_task(db, task["id"])
        assert updated["status"] == "canceled"
        assert updated["pending_command"] == ""
        assert _last_messages_without_turn_state(websocket.messages, 1)[0]["content"] == "已取消"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_during_llm_generation_persists_terminal_turn_state(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    runtime = FakeRuntime()
    runtime.db = db
    websocket = FakeWebSocket()
    started = asyncio.Event()

    class CancellableLLM(FakeLLM):
        async def generate_command(self, user_input: str, history=None):
            started.set()
            await asyncio.Event().wait()

    runtime.llm = CancellableLLM()
    try:
        task = asyncio.create_task(
            _handle_chat_message(
                websocket,
                runtime,
                "sess_cancel_llm",
                "查询服务器状态",
                confirm_mode="auto_safe",
                target="unit-host",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        tasks = await get_session_tasks(
            db,
            "sess_cancel_llm",
            channel="chat",
            include_completed=True,
        )
        assert len(tasks) == 1
        assert tasks[0]["status"] == "canceled"
        assert tasks[0]["completed_at"]
        assert runtime.running_task_ids == {}
        turn_states = [message for message in websocket.messages if message["type"] == "turn_state"]
        assert turn_states[-1]["status"] == "canceled"
        assert turn_states[-1]["active"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_web_chat_raw_mode_does_not_add_llm_result_analysis(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    runtime.llm.command_response = {
        "command": "ssh unit-host 'df -h'",
        "intent": "查看磁盘空间",
        "explanation": "df -h 以易读单位显示磁盘容量和使用率",
        "response_mode": "raw",
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_raw",
        "查询一下磁盘空间",
        confirm_mode="auto_safe",
        target="unit-host",
    )
    await _drain_runtime_tasks(runtime)

    assert runtime.executor.execute_calls == 1
    assert runtime.llm.analysis_calls == []
    assert "task_step" not in _message_types(websocket.messages)
    assert _message_types(websocket.messages) == [
        "user_message",
        "system",
        "agent",
        "command_preview",
        "system",
        "execution_status",
        "execution_status",
        "execution_result",
        "execution_status",
    ]


@pytest.mark.asyncio
async def test_web_chat_analyze_mode_adds_llm_result_analysis(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    runtime.llm.command_response = {
        "command": "ssh unit-host 'df -h'",
        "intent": "查看磁盘空间",
        "explanation": "df -h 以易读单位显示磁盘容量和使用率",
        "response_mode": "analyze",
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_analysis",
        "查一下磁盘空间",
        confirm_mode="auto_safe",
        target="unit-host",
    )
    await _drain_runtime_tasks(runtime)

    assert runtime.executor.execute_calls == 1
    assert len(runtime.llm.analysis_calls) == 1
    analysis_call = runtime.llm.analysis_calls[0]
    assert analysis_call["user_input"] == "查一下磁盘空间"
    assert analysis_call["command"] == "df -h"
    assert analysis_call["output"] == "ok"
    assert analysis_call["exit_code"] == 0
    assert analysis_call["timed_out"] is False
    assert _message_types(websocket.messages) == [
        "user_message",
        "system",
        "agent",
        "command_preview",
        "system",
        "execution_status",
        "execution_status",
        "execution_result",
        "system",
        "agent",
    ]
    last_two = _last_messages_without_turn_state(websocket.messages, 2)
    assert last_two[0]["content"] == "正在分析结果..."
    assert last_two[1]["content"] == "分析：磁盘空间正常"
    assert _turn_state_values(websocket.messages)[-3:] == [
        ("executing", "正在执行命令"),
        ("analyzing", "正在分析结果"),
        ("completed", "任务完成"),
    ]


@pytest.mark.asyncio
async def test_web_chat_raw_summary_transitions_from_execution_to_conclusion(monkeypatch) -> None:
    class ResultSummaryLLM(FakeLLM):
        async def summarize_task_result(self, **kwargs):
            return "unit-host 的磁盘空间检查已完成，当前状态正常。"

    runtime = FakeRuntime()
    runtime.llm = ResultSummaryLLM()
    runtime.llm.command_response = {
        "command": "ssh unit-host 'df -h'",
        "intent": "查看磁盘空间",
        "explanation": "df -h 以易读单位显示磁盘容量和使用率",
        "response_mode": "raw",
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_raw_summary_state",
        "查一下磁盘空间是否正常",
        confirm_mode="auto_safe",
        target="unit-host",
    )
    await _drain_runtime_tasks(runtime)

    assert _turn_state_values(websocket.messages)[-3:] == [
        ("executing", "正在执行命令"),
        ("analyzing", "正在生成结论"),
        ("completed", "任务完成"),
    ]
    assert any(
        message["type"] == "agent" and "磁盘空间检查已完成" in message["content"]
        for message in websocket.messages
    )


@pytest.mark.asyncio
async def test_web_chat_can_plan_next_step_after_analysis(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    runtime.llm.command_response = {
        "command": "ssh unit-host 'df -h'",
        "intent": "先查看磁盘空间",
        "explanation": "df -h 以易读单位显示磁盘容量和使用率",
        "response_mode": "investigate",
    }
    runtime.llm.next_step_response = {
        "done": False,
        "summary": "还需要查看系统负载辅助判断资源情况",
        "next_command": "ssh unit-host 'uptime'",
        "next_intent": "查看系统负载",
        "next_explanation": "uptime 显示运行时间和 1/5/15 分钟平均负载",
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_multistep",
        "综合看一下资源情况",
        confirm_mode="interactive",
        target="unit-host",
    )
    assert "sess_multistep:chat" in runtime.pending_commands

    await _handle_confirm(websocket, runtime, "sess_multistep", confirmed=True, channel="chat")
    await _drain_runtime_tasks(runtime)

    next_pending = runtime.pending_commands["sess_multistep:chat"]
    assert next_pending.actual_command == "uptime"
    assert next_pending.step_index == 2
    assert next_pending.max_steps == 0
    assert next_pending.user_input == "综合看一下资源情况"
    assert next_pending.confirm_mode == "interactive"
    last_two = _last_messages_without_turn_state(websocket.messages, 2)
    assert last_two[0]["type"] == "command_preview"
    assert last_two[0]["command"] == "uptime"
    assert last_two[1]["type"] == "confirm_prompt"


@pytest.mark.asyncio
async def test_web_chat_reason_diagnosis_uses_structured_next_step_protocol(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    runtime.llm.command_response = {
        "command": "ssh unit-host 'journalctl -u mysql.service --no-pager -n 200'",
        "intent": "查看 MySQL 服务日志",
        "explanation": "先从 systemd 日志定位启动失败原因",
        "response_mode": "analyze",
    }
    runtime.llm.analysis_response = (
        "journalctl 中没有 MySQL 日志，需要继续检查独立错误日志。\n\n"
        "```json\n"
        '{"command":"ssh unit-host \'tail -n 200 /var/log/mysql/error.log\'",'
        '"intent":"查看 MySQL 错误日志","response_mode":"workflow"}'
        "\n```"
    )
    runtime.llm.next_step_response = {
        "done": False,
        "summary": "systemd 日志不足，需要继续检查 MySQL 独立错误日志",
        "next_command": (
            "ssh unit-host 'tail -n 200 /var/log/mysql/error.log 2>/dev/null "
            "|| tail -n 200 /var/log/mysqld.log 2>/dev/null'"
        ),
        "next_intent": "查看 MySQL 独立错误日志",
        "next_explanation": "依次检查常见的 MySQL 错误日志路径",
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_reason_diagnosis",
        "看看具体什么原因",
        confirm_mode="interactive",
        target="unit-host",
    )
    await _handle_confirm(
        websocket,
        runtime,
        "sess_reason_diagnosis",
        confirmed=True,
        channel="chat",
    )
    await _drain_runtime_tasks(runtime)

    assert len(runtime.llm.analysis_calls) == 1
    assert len(runtime.llm.next_step_calls) == 1
    next_pending = runtime.pending_commands["sess_reason_diagnosis:chat"]
    assert next_pending.response_mode == "investigate"
    assert next_pending.actual_command.startswith("tail -n 200 /var/log/mysql/error.log")
    visible_agent_text = "\n".join(
        message.get("content", "")
        for message in websocket.messages
        if message["type"] == "agent"
    )
    assert "journalctl 中没有 MySQL 日志" in visible_agent_text
    assert '"command"' not in visible_agent_text
    assert "```json" not in visible_agent_text
    assert _turn_state_values(websocket.messages)[-2:] == [
        ("analyzing", "正在判断下一步"),
        ("waiting_confirm", "等待人工确认"),
    ]


@pytest.mark.asyncio
async def test_workflow_next_step_failure_closes_task(tmp_path) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    runtime = FakeRuntime()
    runtime.db = db
    websocket = FakeWebSocket()

    class FailingDecisionLLM(FakeLLM):
        async def decide_next_step(self, **kwargs):
            raise RuntimeError("decision unavailable")

    runtime.llm = FailingDecisionLLM()
    runtime.llm.command_response = {
        "command": "ssh unit-host 'df -h'",
        "intent": "检查磁盘后继续诊断",
        "explanation": "读取磁盘使用情况",
        "response_mode": "investigate",
    }
    try:
        await _handle_chat_message(
            websocket,
            runtime,
            "sess_decision_failure",
            "综合排查磁盘问题",
            confirm_mode="auto_safe",
            target="unit-host",
        )
        await _drain_runtime_tasks(runtime)

        assert await get_session_tasks(db, "sess_decision_failure", channel="chat") == []
        tasks = await get_session_tasks(
            db,
            "sess_decision_failure",
            channel="chat",
            include_completed=True,
        )
        assert tasks[0]["status"] == "failed"
        assert tasks[0]["completed_at"]
        turn_states = [message for message in websocket.messages if message["type"] == "turn_state"]
        assert turn_states[-1]["status"] == "failed"
        assert turn_states[-1]["active"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_web_session_pending_state_restores_chat_confirmation(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    runtime.llm.command_response = {
        "command": "ssh unit-host 'df -h'",
        "intent": "查看磁盘空间",
        "explanation": "df -h 显示磁盘容量和使用率",
        "response_mode": "raw",
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_pending_restore",
        "查一下磁盘空间",
        confirm_mode="interactive",
        target="unit-host",
    )

    pending = _session_pending_state(runtime, "sess_pending_restore")
    assert pending["chat"]["command"] == "df -h"
    assert pending["chat"]["target"] == "unit-host"
    assert pending["chat"]["confirm_mode"] == "interactive"
    assert pending["chat"]["risk_level"] == "safe"


@pytest.mark.asyncio
async def test_web_confirm_cannot_target_another_sessions_pending_command(monkeypatch) -> None:
    runtime = FakeRuntime()
    websocket = FakeWebSocket()
    first = PendingCommand(
        raw="ssh unit-host 'df -h'",
        target="unit-host",
        target_env="test",
        executor="ssh",
        actual_command="df -h",
        task_id="task_first",
    )
    second = PendingCommand(
        raw="ssh unit-host 'uptime'",
        target="unit-host",
        target_env="test",
        executor="ssh",
        actual_command="uptime",
        task_id="task_second",
    )
    runtime.pending_commands["sess_a:chat"] = first
    runtime.pending_commands["sess_b:chat"] = second

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_confirm(
        websocket,
        runtime,
        "sess_current",
        confirmed=True,
        channel="chat",
        task_id="task_second",
    )
    await _drain_runtime_tasks(runtime)

    assert "sess_a:chat" in runtime.pending_commands
    assert "sess_b:chat" in runtime.pending_commands
    assert runtime.executor.execute_calls == 0
    ack = websocket.messages[-1]
    assert ack["type"] == "confirm_ack"
    assert ack["accepted"] is False
    assert ack["status"] == "not_found"


@pytest.mark.asyncio
async def test_web_concurrent_confirm_is_idempotent_and_acknowledged(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    runtime = FakeRuntime()
    runtime.db = db
    first_socket = FakeWebSocket()
    second_socket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)
    try:
        await ensure_session(db, "sess_double_confirm", session_type="command")
        task = await create_task(
            db,
            session_id="sess_double_confirm",
            channel="command",
            title="查看运行时间",
            confirm_mode="interactive",
        )
        await update_task(
            db,
            task["id"],
            status="waiting_confirm",
            pending_command="uptime",
            pending_target="unit-host",
        )
        command = PendingCommand(
            raw="ssh unit-host 'uptime'",
            target="unit-host",
            target_env="test",
            executor="ssh",
            actual_command="uptime",
            confirm_mode="interactive",
            task_id=task["id"],
        )
        runtime.pending_commands["sess_double_confirm:command"] = command

        await asyncio.gather(
            _handle_confirm(
                first_socket,
                runtime,
                "sess_double_confirm",
                confirmed=True,
                channel="command",
                task_id=task["id"],
                operation_id=task["id"],
                request_id="request-first",
            ),
            _handle_confirm(
                second_socket,
                runtime,
                "sess_double_confirm",
                confirmed=True,
                channel="command",
                task_id=task["id"],
                operation_id=task["id"],
                request_id="request-second",
            ),
        )
        await _drain_runtime_tasks(runtime)

        assert runtime.executor.execute_calls == 1
        acknowledgements = [
            message
            for message in [*first_socket.messages, *second_socket.messages]
            if message["type"] == "confirm_ack"
        ]
        assert len(acknowledgements) == 2
        assert {ack["request_id"] for ack in acknowledgements} == {
            "request-first",
            "request-second",
        }
        assert sum(not ack["duplicate"] for ack in acknowledgements) == 1
        assert all(ack["accepted"] for ack in acknowledgements)
        updated = await get_task(db, task["id"])
        assert updated["status"] == "success"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_web_duplicate_confirm_after_completion_returns_current_state(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "shell_agent.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    runtime = FakeRuntime()
    runtime.db = db
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)
    try:
        await ensure_session(db, "sess_repeat_confirm", session_type="command")
        task = await create_task(
            db,
            "sess_repeat_confirm",
            "command",
            title="查看磁盘",
            confirm_mode="interactive",
        )
        await update_task(
            db,
            task["id"],
            status="waiting_confirm",
            pending_command="df -h",
            pending_target="unit-host",
        )
        runtime.pending_commands["sess_repeat_confirm:command"] = PendingCommand(
            raw="ssh unit-host 'df -h'",
            target="unit-host",
            target_env="test",
            executor="ssh",
            actual_command="df -h",
            task_id=task["id"],
        )

        await _handle_confirm(
            websocket,
            runtime,
            "sess_repeat_confirm",
            confirmed=True,
            channel="command",
            task_id=task["id"],
            request_id="first-click",
        )
        await _drain_runtime_tasks(runtime)
        await _handle_confirm(
            websocket,
            runtime,
            "sess_repeat_confirm",
            confirmed=True,
            channel="command",
            task_id=task["id"],
            request_id="second-click",
        )

        assert runtime.executor.execute_calls == 1
        ack = websocket.messages[-1]
        assert ack["type"] == "confirm_ack"
        assert ack["request_id"] == "second-click"
        assert ack["accepted"] is True
        assert ack["duplicate"] is True
        assert ack["status"] == "success"
        assert "无待确认" not in ack["content"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_web_chat_collects_multiple_servers_without_analysis(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.servers = {"dev-01": object(), "dev-02": object()}
    runtime.llm = FakeLLM()
    runtime.llm.command_response = {
        "command": "ssh dev-01 'ps -ef | grep java | grep -v grep'",
        "intent": "查看 dev-01 上运行的 Java 进程",
        "explanation": "先查看 dev-01，之后继续查看 dev-02。",
        "response_mode": "raw",
    }
    runtime.llm.next_step_response = {
        "done": False,
        "summary": "继续查看 dev-02",
        "next_command": "ssh dev-02 'ps -ef | grep java | grep -v grep'",
        "next_intent": "查看 dev-02 上运行的 Java 进程",
        "next_explanation": "同样使用 ps 和 grep 查看 Java 进程。",
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_collect",
        "看下dev-01和dev-02上面运行了哪些java程序",
        confirm_mode="interactive",
        target="dev-01",
    )

    first_pending = runtime.pending_commands["sess_collect:chat"]
    assert first_pending.target == "dev-01"
    assert first_pending.source == "skill"
    assert first_pending.skill_name == "java_processes"
    assert first_pending.response_mode == "workflow"
    assert first_pending.max_steps == 2
    assert first_pending.step_queue == [
        {
            "command": "ssh dev-02 'ps -ef | grep java | grep -v grep'",
            "intent": "查看 dev-02 上运行的 Java 进程",
            "explanation": "使用 ps -ef 列出进程，通过 grep java 过滤 Java 进程，并排除 grep 命令本身。",
            "skill_step_name": "Java 进程",
        }
    ]

    await _handle_confirm(websocket, runtime, "sess_collect", confirmed=True, channel="chat")
    await _drain_runtime_tasks(runtime)

    assert runtime.llm.analysis_calls == []
    assert runtime.llm.next_step_calls == []
    next_pending = runtime.pending_commands["sess_collect:chat"]
    assert next_pending.target == "dev-02"
    assert next_pending.actual_command == "ps -ef | grep java | grep -v grep"
    assert next_pending.response_mode == "workflow"
    assert next_pending.skill_name == "java_processes"
    assert next_pending.step_index == 2
    last_two = _last_messages_without_turn_state(websocket.messages, 2)
    assert last_two[0]["type"] == "command_preview"
    assert last_two[0]["target"] == "dev-02"
    assert last_two[1]["type"] == "confirm_prompt"


@pytest.mark.asyncio
async def test_web_chat_uses_planned_step_queue_before_next_step_llm(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    runtime.llm.command_response = {
        "intent": "查看资源情况",
        "response_mode": "workflow",
        "steps": [
            {
                "command": "ssh unit-host 'df -h'",
                "intent": "查看磁盘",
                "explanation": "df -h 显示磁盘使用率",
            },
            {
                "command": "ssh unit-host 'uptime'",
                "intent": "查看负载",
                "explanation": "uptime 显示系统负载",
            },
        ],
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_queue",
        "请按计划检查系统",
        confirm_mode="interactive",
        target="unit-host",
    )

    first_pending = runtime.pending_commands["sess_queue:chat"]
    assert first_pending.actual_command == "df -h"
    assert first_pending.step_queue == [
        {
            "command": "ssh unit-host 'uptime'",
            "intent": "查看负载",
            "explanation": "uptime 显示系统负载",
        }
    ]
    assert any(
        message["type"] == "task_step"
        and message["step_index"] == 1
        and message["status"] == "pending"
        for message in websocket.messages
    )

    await _handle_confirm(websocket, runtime, "sess_queue", confirmed=True, channel="chat")
    await _drain_runtime_tasks(runtime)

    assert runtime.llm.next_step_calls == []
    next_pending = runtime.pending_commands["sess_queue:chat"]
    assert next_pending.actual_command == "uptime"
    assert next_pending.step_index == 2
    assert next_pending.step_queue == []
    task_steps = [message for message in websocket.messages if message["type"] == "task_step"]
    assert [step["status"] for step in task_steps[:3]] == ["pending", "running", "success"]
    assert any(
        step["step_index"] == 2 and step["status"] == "pending"
        for step in task_steps
    )
    last_two = _last_messages_without_turn_state(websocket.messages, 2)
    assert last_two[0]["type"] == "command_preview"
    assert last_two[0]["command"] == "uptime"


@pytest.mark.asyncio
async def test_web_chat_workflow_uses_previous_output_for_next_command(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    runtime.llm.command_response = {
        "command": "ssh unit-host 'ls -lht /data/app/app/logs/ | head -n 15'",
        "intent": "先确认当前最新日志文件",
        "explanation": "按修改时间查看 logs 目录，找到最新日志文件。",
        "response_mode": "raw",
    }
    runtime.llm.next_step_response = {
        "done": False,
        "summary": "已经找到最新日志 app.log，继续读取内容",
        "next_command": "ssh unit-host 'tail -n 200 /data/app/app/logs/app.log'",
        "next_intent": "读取最新日志内容",
        "next_explanation": "使用 tail 读取最新日志末尾 200 行，避免输出过大。",
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_workflow",
        "看一下 camel 服务的日志内容",
        confirm_mode="interactive",
        target="unit-host",
    )

    first_pending = runtime.pending_commands["sess_workflow:chat"]
    assert first_pending.response_mode == "workflow"

    await _handle_confirm(websocket, runtime, "sess_workflow", confirmed=True, channel="chat")
    await _drain_runtime_tasks(runtime)

    assert runtime.llm.analysis_calls == []
    assert len(runtime.llm.next_step_calls) == 1
    next_pending = runtime.pending_commands["sess_workflow:chat"]
    assert next_pending.actual_command == "tail -n 200 /data/app/app/logs/app.log"
    assert next_pending.response_mode == "workflow"
    assert next_pending.step_index == 2
    last_two = _last_messages_without_turn_state(websocket.messages, 2)
    assert last_two[0]["type"] == "command_preview"
    assert last_two[0]["command"] == "tail -n 200 /data/app/app/logs/app.log"
    assert last_two[1]["type"] == "confirm_prompt"
    assert _turn_state_values(websocket.messages)[-2:] == [
        ("analyzing", "正在判断下一步"),
        ("waiting_confirm", "等待人工确认"),
    ]


@pytest.mark.asyncio
async def test_web_chat_workflow_marks_final_conclusion_before_completion(monkeypatch) -> None:
    runtime = FakeRuntime()
    runtime.llm = FakeLLM()
    runtime.llm.command_response = {
        "command": "ssh unit-host 'df -h'",
        "intent": "检查磁盘并给出结论",
        "explanation": "读取磁盘使用情况后判断任务是否完成",
        "response_mode": "workflow",
    }
    runtime.llm.next_step_response = {
        "done": True,
        "summary": "磁盘检查完成，无需继续执行。",
    }
    websocket = FakeWebSocket()

    async def fake_write_audit(rt, command, executed, **kwargs) -> None:
        rt.audit_records.append({"command": command.actual_command, "executed": executed, **kwargs})

    monkeypatch.setattr("shell_agent.web.api._write_audit", fake_write_audit)

    await _handle_chat_message(
        websocket,
        runtime,
        "sess_workflow_final_state",
        "检查磁盘并给出结论",
        confirm_mode="auto_safe",
        target="unit-host",
    )
    await _drain_runtime_tasks(runtime)

    assert _turn_state_values(websocket.messages)[-4:] == [
        ("executing", "正在执行命令"),
        ("analyzing", "正在判断下一步"),
        ("analyzing", "正在生成最终结论"),
        ("completed", "任务完成"),
    ]
