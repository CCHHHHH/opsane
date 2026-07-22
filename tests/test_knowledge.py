from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from shell_agent.knowledge.learning import learn_from_task
from shell_agent.knowledge.redaction import REDACTED, SecretRedactor
from shell_agent.knowledge.resolver import KnowledgeResolver
from shell_agent.storage.database import connect, init_db
from shell_agent.storage.memories import (
    get_memory,
    maintain_memories,
    upsert_memory,
)
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
async def test_memory_issues_are_physically_purged_after_thirty_days(tmp_path: Path) -> None:
    db_path = tmp_path / "memory-retention.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    stale = await upsert_memory(
        db,
        subject="old-service",
        predicate="status",
        value="retired",
        status="stale",
    )
    first_conflict = await upsert_memory(
        db,
        subject="mysql",
        predicate="server",
        value="dev-01",
        status="inferred",
    )
    recent_conflict = await upsert_memory(
        db,
        subject="mysql",
        predicate="server",
        value="dev-02",
        status="inferred",
    )
    newly_expired = await upsert_memory(
        db,
        subject="temporary-port",
        predicate="value",
        value="8080",
        status="confirmed",
        expires_at="2000-01-01T00:00:00",
    )
    now = datetime.now()
    old = (now - timedelta(days=31)).isoformat(timespec="seconds")
    recent = (now - timedelta(days=29)).isoformat(timespec="seconds")
    await db.execute(
        "UPDATE global_memories SET updated_at = ? WHERE id IN (?, ?)",
        (old, stale["id"], first_conflict["id"]),
    )
    await db.execute(
        "UPDATE global_memories SET updated_at = ? WHERE id = ?",
        (recent, recent_conflict["id"]),
    )
    await db.commit()

    result = await maintain_memories(db)

    assert result == {"expired": 1, "purged": 2}
    assert await get_memory(db, stale["id"]) is None
    assert await get_memory(db, first_conflict["id"]) is None
    assert (await get_memory(db, recent_conflict["id"]))["status"] == "conflicted"
    assert (await get_memory(db, newly_expired["id"]))["status"] == "stale"
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


class StaticKnowledgeLLM:
    def __init__(self, memories: list[dict]) -> None:
        self.memories = memories

    async def extract_knowledge(self, **kwargs):
        return {"memories": self.memories}


async def _learn_with_memories(
    tmp_path: Path,
    memories: list[dict],
    *,
    services: dict[str, ServiceProfile],
    servers: dict[str, object],
):
    db_path = tmp_path / "grouped-learning.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    await ensure_session(db, "sess-grouped", session_type="chat")
    task = await create_task(db, "sess-grouped", "chat", title="检查服务画像")
    await add_task_event(
        db,
        task["id"],
        "sess-grouped",
        "chat",
        "execution_result",
        status="success",
        step_index=1,
        content="服务检查完成",
        payload={
            "target": memories[0]["target"],
            "command": "systemctl status mysql",
            "exit_code": 0,
            "output": "mysql service is active",
        },
    )
    result = await learn_from_task(
        db,
        StaticKnowledgeLLM(memories),
        task_id=task["id"],
        session_id="sess-grouped",
        user_input="检查 mysql 服务版本和运行方式",
        final_summary="mysql 服务检查完成",
        services=services,
        servers=servers,
    )
    return db, result


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


@pytest.mark.asyncio
async def test_task_learning_merges_profile_changes_for_same_service_instance(tmp_path: Path) -> None:
    common = {
        "type": "fact",
        "subject": "mysql",
        "target": "dev-01",
        "confidence": 0.95,
        "service_id": "mysql",
        "service_name": "MySQL",
    }
    db, result = await _learn_with_memories(
        tmp_path,
        [
            {
                **common,
                "predicate": "version",
                "value": "8.0.46",
                "evidence_summary": "mysql --version 返回 8.0.46",
                "profile_changes": {"servers": ["dev-01"], "version": "8.0.46"},
            },
            {
                **common,
                "predicate": "runtime",
                "value": "systemd",
                "evidence_summary": "systemctl status 显示 mysqld.service",
                "profile_changes": {"servers": ["dev-01"], "runtime": "systemd"},
            },
        ],
        services={"mysql": ServiceProfile(id="mysql", name="MySQL", servers=["dev-01"])},
        servers={"dev-01": object()},
    )

    candidates = await list_profile_candidates(db)
    assert result.candidate_count == 1
    assert len(candidates) == 1
    assert candidates[0]["proposed_changes"] == {
        "servers": ["dev-01"],
        "version": "8.0.46",
        "runtime": "systemd",
    }
    assert len(candidates[0]["source_memory_ids"]) == 2
    assert "8.0.46" in candidates[0]["evidence"]["summary"]
    assert "mysqld.service" in candidates[0]["evidence"]["summary"]
    await db.close()


@pytest.mark.asyncio
async def test_task_learning_separates_same_named_service_on_another_server(tmp_path: Path) -> None:
    db, result = await _learn_with_memories(
        tmp_path,
        [
            {
                "type": "fact",
                "subject": "mysql",
                "predicate": "version",
                "value": "8.0.33",
                "target": "dev-04",
                "confidence": 0.95,
                "evidence_summary": "dev-04 的 mysqld 输出版本 8.0.33",
                "service_id": "mysql",
                "service_name": "MySQL",
                "profile_changes": {
                    "servers": ["dev-04"],
                    "version": "8.0.33",
                },
            }
        ],
        services={"mysql": ServiceProfile(id="mysql", name="MySQL", servers=["aliyun-01"])},
        servers={"aliyun-01": object(), "dev-04": object()},
    )

    candidates = await list_profile_candidates(db)
    assert result.candidate_count == 1
    assert candidates[0]["service_id"] == "mysql-dev-04"
    assert candidates[0]["before_snapshot"] == {}
    assert candidates[0]["proposed_changes"]["servers"] == ["dev-04"]
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


def test_stale_candidate_expires_and_can_rebase_without_field_conflict(tmp_path, monkeypatch) -> None:
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
        client.post(
            "/api/servers",
            json={
                "alias": "dev-02",
                "host": "10.0.0.2",
                "env": "dev",
                "ssh_credential": "ssh",
            },
        )
        created_service = client.post(
            "/api/services",
            json={"id": "mysql", "name": "MySQL", "servers": ["dev-01"]},
        ).json()["service"]

        connection = sqlite3.connect(db_path)
        rows = [
            (
                "candidate-runtime",
                json.dumps({"runtime": "systemd"}),
                json.dumps({"target": "dev-01", "summary": "systemd evidence"}),
                "fingerprint-runtime",
            ),
            (
                "candidate-version",
                json.dumps({"version": "8.0.46"}),
                json.dumps({"target": "dev-01", "summary": "version evidence"}),
                "fingerprint-version",
            ),
            (
                "candidate-other-host",
                json.dumps({"servers": ["dev-02"], "version": "8.0.33"}),
                json.dumps({"target": "dev-02", "summary": "other host evidence"}),
                "fingerprint-other-host",
            ),
        ]
        for candidate_id, changes, evidence, fingerprint in rows:
            connection.execute(
                """
                INSERT INTO service_profile_candidates (
                    id, service_id, service_name, proposed_changes, before_snapshot,
                    evidence, confidence, fingerprint, status, source_memory_ids,
                    source_task_id, created_at
                ) VALUES (?, 'mysql', 'MySQL', ?, ?, ?, 0.95, ?, 'pending', '[]',
                          'task-shared', '2026-07-21T10:00:00')
                """,
                (candidate_id, changes, json.dumps(created_service), evidence, fingerprint),
            )
        connection.commit()
        connection.close()

        accepted = client.post(
            "/api/service-profile-candidates/candidate-runtime/accept", json={}
        )
        assert accepted.status_code == 200
        assert accepted.json()["service"]["revision"] == 2

        expired = client.get(
            "/api/service-profile-candidates", params={"status": "expired"}
        ).json()["candidates"]
        assert {item["id"] for item in expired} == {
            "candidate-version",
            "candidate-other-host",
        }

        expired_accept = client.post(
            "/api/service-profile-candidates/candidate-version/accept", json={}
        )
        assert expired_accept.status_code == 409
        assert "基于 revision=1，当前 revision=2" in expired_accept.json()["detail"]

        rebased = client.post(
            "/api/service-profile-candidates/candidate-version/rebase", json={}
        )
        assert rebased.status_code == 200
        assert rebased.json()["candidate"]["before_snapshot"]["revision"] == 2
        accepted_version = client.post(
            "/api/service-profile-candidates/candidate-version/accept", json={}
        )
        assert accepted_version.status_code == 200
        assert accepted_version.json()["service"]["version"] == "8.0.46"
        assert accepted_version.json()["service"]["revision"] == 3

        mismatched = client.post(
            "/api/service-profile-candidates/candidate-other-host/rebase", json={}
        )
        assert mismatched.status_code == 409
        assert "独立服务实例" in json.dumps(mismatched.json(), ensure_ascii=False)


def test_memory_knowledge_endpoints_paginate_large_collections(tmp_path, monkeypatch) -> None:
    agent_path = _write_runtime_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "data" / "shell_agent.db"

    with TestClient(create_app(str(agent_path))) as client:
        for index in range(25):
            response = client.post(
                "/api/memories",
                json={
                    "subject": f"service-{index:02d}",
                    "predicate": "owner",
                    "value": f"team-{index:02d}",
                    "status": "confirmed",
                },
            )
            assert response.json()["ok"] is True
        for index in range(11):
            response = client.post(
                "/api/memories",
                json={
                    "subject": f"stale-{index:02d}",
                    "predicate": "status",
                    "value": "expired",
                    "status": "stale",
                },
            )
            assert response.json()["ok"] is True

        connection = sqlite3.connect(db_path)
        for index in range(13):
            connection.execute(
                """
                INSERT INTO service_profile_candidates (
                    id, service_name, proposed_changes, fingerprint, status, created_at
                ) VALUES (?, ?, '{}', ?, 'pending', ?)
                """,
                (
                    f"candidate-{index:02d}",
                    f"Service {index:02d}",
                    f"fingerprint-{index:02d}",
                    f"2026-07-21T10:{index:02d}:00",
                ),
            )
        connection.commit()
        connection.close()

        memories = client.get(
            "/api/memories",
            params={"status": "confirmed", "page": 2, "page_size": 10},
        ).json()
        assert len(memories["memories"]) == 10
        assert memories["pagination"] == {
            "page": 2,
            "page_size": 10,
            "total": 25,
            "total_pages": 3,
        }

        issues = client.get(
            "/api/knowledge/conflicts",
            params={"page": 3, "page_size": 5},
        ).json()
        assert len(issues["issues"]) == 1
        assert issues["pagination"] == {
            "page": 3,
            "page_size": 5,
            "total": 11,
            "total_pages": 3,
        }

        candidates = client.get(
            "/api/service-profile-candidates",
            params={"page": 2, "page_size": 5},
        ).json()
        assert len(candidates["candidates"]) == 5
        assert candidates["pagination"] == {
            "page": 2,
            "page_size": 5,
            "total": 13,
            "total_pages": 3,
        }
