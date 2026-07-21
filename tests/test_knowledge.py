from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from shell_agent.knowledge.learning import learn_from_task
from shell_agent.knowledge.redaction import REDACTED, SecretRedactor
from shell_agent.knowledge.resolver import KnowledgeResolver
from shell_agent.storage.database import connect, init_db
from shell_agent.storage.memories import get_memory, upsert_memory
from shell_agent.storage.profile_candidates import list_profile_candidates
from shell_agent.storage.sessions import ensure_session
from shell_agent.storage.tasks import add_task_event, create_task
from shell_agent.utils.config import ServerEntry, ServiceProfile
from shell_agent.web.app import create_app


def _write_runtime_files(root: Path) -> Path:
    config_dir = root / "config"
    data_dir = root / "data"
    config_dir.mkdir()
    data_dir.mkdir()
    agent_path = config_dir / "agent.yaml"
    agent_path.write_text(
        yaml.safe_dump(
            {
                "llm": {"api_key": "test-key", "model": "test-model"},
                "storage": {"sqlite_path": str(data_dir / "shell_agent.db")},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "credentials.yaml").write_text("credentials: []\n", encoding="utf-8")
    (config_dir / "inventory.yaml").write_text("servers: []\nservices: []\n", encoding="utf-8")
    return agent_path


def test_secret_redactor_removes_common_credentials() -> None:
    redactor = SecretRedactor(["known-secret"])
    text = redactor.redact_text(
        "password=known-secret token: abc123 mysqladmin -u root -pOldPass "
        "password 'NewPass' IDENTIFIED BY 'SqlPass' https://user:urlpass@example.com"
    )
    for secret in ("known-secret", "abc123", "OldPass", "NewPass", "SqlPass", "urlpass"):
        assert secret not in text
    assert REDACTED in text


@pytest.mark.asyncio
async def test_memory_conflicts_share_fingerprint_and_become_conflicted(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    first = await upsert_memory(
        db,
        subject="mysql",
        predicate="server",
        value="dev-01",
        status="inferred",
    )
    second = await upsert_memory(
        db,
        subject="mysql",
        predicate="server",
        value="dev-02",
        status="inferred",
    )
    assert first["fingerprint"] == second["fingerprint"]
    assert (await get_memory(db, first["id"]))["status"] == "conflicted"
    assert (await get_memory(db, second["id"]))["status"] == "conflicted"
    await db.close()


@pytest.mark.asyncio
async def test_knowledge_resolver_prefers_profile_and_requires_confirmation_for_memory(tmp_path: Path) -> None:
    db_path = tmp_path / "resolver.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    await upsert_memory(
        db,
        subject="mysql",
        predicate="server",
        value="dev-02",
        target="dev-02",
        status="confirmed",
    )
    servers = {
        "dev-01": ServerEntry(alias="dev-01", host="10.0.0.1", ssh_credential="ssh"),
        "dev-02": ServerEntry(alias="dev-02", host="10.0.0.2", ssh_credential="ssh"),
    }
    services = {"mysql": ServiceProfile(id="mysql", name="MySQL", servers=["dev-01"])}
    memory_resolution = await KnowledgeResolver(
        db, servers=servers, services={}
    ).resolve("重启 mysql")
    assert memory_resolution.resolved_target == "dev-02"
    assert memory_resolution.target_source == "memory"
    assert memory_resolution.requires_target_confirmation is True

    profile_resolution = await KnowledgeResolver(
        db, servers=servers, services=services
    ).resolve("重启 mysql")
    assert profile_resolution.resolved_target == "dev-01"
    assert profile_resolution.target_source == "profile"
    assert profile_resolution.requires_target_confirmation is False
    assert "service=MySQL" in profile_resolution.llm_context
    assert profile_resolution.conflicts
    await db.close()


def test_init_db_migrates_legacy_global_memories(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE global_memories (
            id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
            value TEXT NOT NULL, target TEXT, confidence REAL DEFAULT 1.0,
            source_session_id TEXT, source TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, deleted_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO global_memories (
            id, subject, predicate, value, target, confidence, source,
            created_at, updated_at
        ) VALUES ('legacy-1', 'mysql', 'server', 'dev-01', 'dev-01', 1.0,
                  'manual', '2026-07-01T10:00:00', '2026-07-01T10:00:00')
        """
    )
    connection.commit()
    connection.close()

    import asyncio

    asyncio.run(init_db(str(db_path)))
    connection = sqlite3.connect(db_path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(global_memories)")}
    row = connection.execute("SELECT type, status, observed_at FROM global_memories WHERE id = 'legacy-1'").fetchone()
    connection.close()
    assert {"type", "status", "source_task_id", "expires_at", "fingerprint"} <= columns
    assert row == ("fact", "confirmed", "2026-07-01T10:00:00")


class FakeKnowledgeLLM:
    def __init__(self) -> None:
        self.request: dict | None = None

    async def extract_knowledge(self, **kwargs):
        self.request = kwargs
        return {
            "memories": [
                {
                    "type": "fact",
                    "subject": "mysql",
                    "predicate": "version",
                    "value": "8.0.45",
                    "target": "dev-01",
                    "confidence": 0.96,
                    "evidence_summary": "mysqld --version 返回 8.0.45",
                    "service_id": "mysql",
                    "service_name": "MySQL",
                    "profile_changes": {
                        "servers": ["dev-01"],
                        "runtime": "systemd",
                        "version": "8.0.45",
                        "status_cmd": "systemctl status mysqld",
                    },
                }
            ]
        }


@pytest.mark.asyncio
async def test_completed_task_learning_creates_memory_and_profile_candidate(tmp_path: Path) -> None:
    db_path = tmp_path / "learning.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    await ensure_session(db, "sess-1", session_type="chat")
    task = await create_task(db, "sess-1", "chat", title="检查 MySQL 版本")
    await add_task_event(
        db,
        task["id"],
        "sess-1",
        "chat",
        "execution_result",
        status="success",
        step_index=1,
        content="mysqld Ver 8.0.45",
        payload={
            "target": "dev-01",
            "command": "mysqld --version --password=super-secret",
            "exit_code": 0,
            "output": "mysqld Ver 8.0.45 password=super-secret",
        },
    )
    llm = FakeKnowledgeLLM()
    result = await learn_from_task(
        db,
        llm,
        task_id=task["id"],
        session_id="sess-1",
        user_input="检查 MySQL 版本，密码是 super-secret",
        final_summary="dev-01 上是 MySQL 8.0.45",
        services={"mysql": ServiceProfile(id="mysql", name="MySQL", servers=["dev-01"])},
        servers={"dev-01": object()},
        secret_values=["super-secret"],
    )
    assert result.memory_count == 1
    assert result.candidate_count == 1
    assert llm.request is not None
    assert "super-secret" not in json.dumps(llm.request, ensure_ascii=False)
    candidates = await list_profile_candidates(db)
    assert candidates[0]["proposed_changes"]["version"] == "8.0.45"
    assert candidates[0]["before_snapshot"]["revision"] == 1
    await db.close()


def test_profile_candidate_accept_updates_inventory_and_promotes_memory(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "data" / "shell_agent.db"

    with TestClient(create_app(str(agent_path))) as client:
        client.post(
            "/api/servers",
            json={
                "alias": "dev-01",
                "host": "10.0.0.1",
                "env": "dev",
                "ssh_credential": "ssh",
            },
        )
        created_service = client.post(
            "/api/services",
            json={"id": "mysql", "name": "MySQL", "servers": ["dev-01"]},
        ).json()["service"]
        memory = client.post(
            "/api/memories",
            json={
                "subject": "mysql",
                "predicate": "version",
                "value": "8.0.45",
                "target": "dev-01",
                "status": "inferred",
            },
        ).json()["memory"]

        connection = sqlite3.connect(db_path)
        connection.execute(
            """
            INSERT INTO service_profile_candidates (
                id, service_id, service_name, proposed_changes, before_snapshot,
                evidence, confidence, fingerprint, status, source_memory_ids,
                source_task_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "candidate-1",
                "mysql",
                "MySQL",
                json.dumps({"runtime": "systemd", "version": "8.0.45"}),
                json.dumps(created_service),
                json.dumps({"target": "dev-01", "summary": "版本命令执行成功"}),
                0.96,
                "fingerprint-1",
                "pending",
                json.dumps([memory["id"]]),
                "task-1",
                "2026-07-13T10:00:00",
            ),
        )
        connection.commit()
        connection.close()

        accepted = client.post("/api/service-profile-candidates/candidate-1/accept", json={})
        assert accepted.status_code == 200
        assert accepted.json()["ok"] is True
        service = accepted.json()["service"]
        assert service["runtime"] == "systemd"
        assert service["version"] == "8.0.45"
        assert service["verification_status"] == "verified"
        assert service["revision"] == 2
        promoted = client.get("/api/memories?status=promoted").json()["memories"]
        assert [item["id"] for item in promoted] == [memory["id"]]
        assert client.get("/api/service-profile-candidates").json()["candidates"] == []
        assert client.get("/api/audit?target=mysql").json()["records"][0]["source"] == "service_profile_review"
