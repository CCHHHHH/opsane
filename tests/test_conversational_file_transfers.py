from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from shell_agent.executors.ssh import SSHExecutor
from shell_agent.storage.database import connect, init_db
from shell_agent.storage.file_transfers import (
    claim_file_transfer,
    confirm_file_transfer,
    create_file_transfer,
    get_file_transfer,
    interrupt_running_file_transfers,
)
from shell_agent.storage.session_files import create_session_file
from shell_agent.storage.sessions import ensure_session
from shell_agent.web.app import create_app
from shell_agent.web.ws.file_transfer_intent import (
    resolve_conversational_file_transfer,
)


def _runtime_config(root: Path) -> Path:
    config = root / "config"
    data = root / "data"
    config.mkdir()
    data.mkdir()
    path = config / "agent.yaml"
    path.write_text(
        yaml.safe_dump({
            "llm": {"api_key": "test", "model": "test"},
            "storage": {"sqlite_path": str(data / "shell_agent.db")},
            "ssh": {"default_timeout": 1},
        }),
        encoding="utf-8",
    )
    (config / "credentials.yaml").write_text(
        "credentials: []\n", encoding="utf-8"
    )
    (config / "inventory.yaml").write_text(
        yaml.safe_dump({
            "servers": [{
                "alias": "dev-01",
                "host": "127.0.0.1",
                "port": 22,
                "env": "dev",
                "ssh_credential": "unused",
            }]
        }),
        encoding="utf-8",
    )
    return path


def _receive_ws_type(websocket, expected: str, *, request_id: str = "") -> dict:
    for _ in range(50):
        message = websocket.receive_json()
        if message.get("type") != expected:
            continue
        if request_id and message.get("request_id") != request_id:
            continue
        return message
    raise AssertionError(f"WebSocket 未收到事件: {expected}")


async def _session_file(db, root: Path, *, session_id: str, name: str) -> dict:
    path = root / name
    path.write_bytes(b"conversation-upload")
    return await create_session_file(
        db,
        session_id=session_id,
        original_name=name,
        stored_path=str(path),
        media_type="application/octet-stream",
        extension=path.suffix,
        size=path.stat().st_size,
        sha256="test-sha256",
    )


@pytest.mark.asyncio
async def test_waiting_transfer_survives_restart_and_double_confirm_executes_once(
    tmp_path: Path,
) -> None:
    """A refresh/restart must not lose approval state or duplicate the write."""
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await ensure_session(db, "sess-conversation", session_type="chat")
        file = await _session_file(
            db,
            tmp_path,
            session_id="sess-conversation",
            name="app.jar",
        )
        transfer, created = await create_file_transfer(
            db,
            request_id="conversation-request",
            session_id="sess-conversation",
            file_id=file["id"],
            file_name="app.jar",
            target="dev-01",
            target_env="dev",
            remote_dir="/tmp/uploads",
            remote_name="app.jar",
            remote_path="/tmp/uploads/app.jar",
            overwrite=False,
            size=file["size"],
            sha256=file["sha256"],
            initial_status="waiting_confirm",
            source="conversation",
            turn_id="turn-conversation",
        )
        assert created is True

        # Startup reconciliation only interrupts work which may already have
        # produced a remote side effect. Approval-only records remain durable.
        assert await interrupt_running_file_transfers(db) == []
        restored = await get_file_transfer(db, transfer["id"])
        assert restored is not None
        assert restored["status"] == "waiting_confirm"

        decisions = await asyncio.gather(
            confirm_file_transfer(
                db,
                transfer["id"],
                "sess-conversation",
                confirmed=True,
            ),
            confirm_file_transfer(
                db,
                transfer["id"],
                "sess-conversation",
                confirmed=True,
            ),
        )
        assert sorted(changed for changed, _ in decisions) == [False, True]

        claims = await asyncio.gather(
            claim_file_transfer(db, transfer["id"], "sess-conversation"),
            claim_file_transfer(db, transfer["id"], "sess-conversation"),
        )
        assert sorted(claimed for claimed, _ in claims) == [False, True]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_conversational_transfer_requires_unambiguous_session_objects(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await ensure_session(db, "sess-conversation", session_type="chat")
        await _session_file(
            db,
            tmp_path,
            session_id="sess-conversation",
            name="app.jar",
        )
        runtime = SimpleNamespace(
            db=db,
            executor=SimpleNamespace(servers={"dev-01": object(), "prod-01": object()}),
        )

        resolved = await resolve_conversational_file_transfer(
            runtime,
            "sess-conversation",
            "把 app.jar 上传到 dev-01 的 /tmp/uploads 目录",
        )
        assert resolved.attempted is True
        assert resolved.clarification == ""
        assert resolved.intent is not None
        assert resolved.intent.file_name == "app.jar"
        assert resolved.intent.target == "dev-01"
        assert resolved.intent.remote_dir == "/tmp/uploads"
        assert resolved.intent.overwrite is False

        missing_target = await resolve_conversational_file_transfer(
            runtime,
            "sess-conversation",
            "把 app.jar 上传到 /tmp/uploads 目录",
        )
        assert missing_target.attempted is True
        assert missing_target.intent is None
        assert "服务器别名" in missing_target.clarification

        ambiguous_target = await resolve_conversational_file_transfer(
            runtime,
            "sess-conversation",
            "把 app.jar 上传到 dev-01 或 prod-01 的 /tmp/uploads 目录",
        )
        assert ambiguous_target.attempted is True
        assert ambiguous_target.intent is None
        assert "多个目标服务器" in ambiguous_target.clarification
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_conversational_transfer_resolves_partial_war_name_and_chinese_path_suffix(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        session_id = "sess-partial-war"
        await ensure_session(db, session_id, session_type="chat")
        await _session_file(
            db,
            tmp_path,
            session_id=session_id,
            name="bedcare-mock.jar",
        )
        await _session_file(
            db,
            tmp_path,
            session_id=session_id,
            name="IoT中台打包安装部署文档.doc",
        )
        platform_war = await _session_file(
            db,
            tmp_path,
            session_id=session_id,
            name="avatar-iot-platform.war",
        )
        runtime = SimpleNamespace(
            db=db,
            executor=SimpleNamespace(servers={"dev-01": object()}),
        )

        resolved = await resolve_conversational_file_transfer(
            runtime,
            session_id,
            "帮我把 platform 这个 war 包放到 dev-01 服务器的/data/app/test目录下",
        )
        assert resolved.attempted is True
        assert resolved.clarification == ""
        assert resolved.intent is not None
        assert resolved.intent.file_id == platform_war["id"]
        assert resolved.intent.file_name == "avatar-iot-platform.war"
        assert resolved.intent.target == "dev-01"
        assert resolved.intent.remote_dir == "/data/app/test"
        assert resolved.intent.overwrite is False

        # “部署包”是文件类别，不应被误判成执行部署动作。
        deployment_package = await resolve_conversational_file_transfer(
            runtime,
            session_id,
            "把 platform 这个部署包放到 dev-01 的 /data/app/test 目录下",
        )
        assert deployment_package.intent is not None
        assert deployment_package.intent.file_id == platform_war["id"]

        await _session_file(
            db,
            tmp_path,
            session_id=session_id,
            name="platform-admin.war",
        )
        ambiguous = await resolve_conversational_file_transfer(
            runtime,
            session_id,
            "把 platform 这个 war 包放到 dev-01 的 /data/app/test 目录下",
        )
        assert ambiguous.attempted is True
        assert ambiguous.intent is None
        assert "多个可能的会话文件" in ambiguous.clarification
        assert "avatar-iot-platform.war" in ambiguous.clarification
        assert "platform-admin.war" in ambiguous.clarification
    finally:
        await db.close()


def test_waiting_preview_restores_after_websocket_reconnect_and_conflicting_ack_rejects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _runtime_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = 0

    async def forbidden_upload(self, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("rejected transfer must never reach SFTP")

    monkeypatch.setattr(SSHExecutor, "upload_file_verified", forbidden_upload)

    with TestClient(create_app(str(config_path))) as client:
        session = client.post(
            "/api/sessions", json={"type": "chat", "title": "reconnect"}
        ).json()["session"]
        client.post(
            f"/api/sessions/{session['id']}/files",
            files={"files": ("app.jar", b"fake-jar", "application/java-archive")},
        )

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({
                "type": "chat",
                "session_id": session["id"],
                "message": "把 app.jar 上传到 dev-01 的 /tmp/releases",
                "confirm_mode": "full_access",
            })
            transfer = _receive_ws_type(
                websocket, "file_transfer_preview"
            )["transfer"]
            assert transfer["status"] == "waiting_confirm"
            assert calls == 0

        # A browser refresh creates a new socket. The actionable confirmation
        # must come from durable state, not the old connection's memory.
        with client.websocket_connect("/ws/chat") as refreshed:
            refreshed.send_json({
                "type": "subscribe",
                "session_id": session["id"],
                "channel": "chat",
            })
            sync = _receive_ws_type(refreshed, "session_sync")
            assert sync["pending"]["file_transfer"]["id"] == transfer["id"]
            assert any(
                task["status"] == "waiting_confirm" for task in sync["tasks"]
            )

            refreshed.send_json({
                "type": "file_transfer_confirm",
                "session_id": session["id"],
                "transfer_id": transfer["id"],
                "confirmed": False,
                "request_id": "reject-first",
            })
            rejected = _receive_ws_type(
                refreshed,
                "file_transfer_confirm_ack",
                request_id="reject-first",
            )
            assert rejected["accepted"] is True
            assert rejected["status"] == "cancelled"

            refreshed.send_json({
                "type": "file_transfer_confirm",
                "session_id": session["id"],
                "transfer_id": transfer["id"],
                "confirmed": True,
                "request_id": "confirm-opposite",
            })
            conflict = _receive_ws_type(
                refreshed,
                "file_transfer_confirm_ack",
                request_id="confirm-opposite",
            )
            assert conflict["accepted"] is False
            assert conflict["status"] == "conflict"
            assert calls == 0


def test_failed_conversational_transfer_persists_failure_without_success_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _runtime_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = 0

    async def failed_upload(self, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated remote disk full")

    monkeypatch.setattr(SSHExecutor, "upload_file_verified", failed_upload)

    with TestClient(create_app(str(config_path))) as client:
        session = client.post(
            "/api/sessions", json={"type": "chat", "title": "failure"}
        ).json()["session"]
        client.post(
            f"/api/sessions/{session['id']}/files",
            files={"files": ("app.jar", b"fake-jar", "application/java-archive")},
        )
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({
                "type": "chat",
                "session_id": session["id"],
                "message": "把 app.jar 上传到 dev-01 的 /tmp/releases",
            })
            transfer = _receive_ws_type(
                websocket, "file_transfer_preview"
            )["transfer"]
            websocket.send_json({
                "type": "file_transfer_confirm",
                "session_id": session["id"],
                "transfer_id": transfer["id"],
                "confirmed": True,
                "request_id": "confirm-failure",
            })
            ack = _receive_ws_type(
                websocket,
                "file_transfer_confirm_ack",
                request_id="confirm-failure",
            )
            assert ack["accepted"] is True

        final = transfer
        for _ in range(100):
            final = client.get(
                f"/api/sessions/{session['id']}/file-transfers"
            ).json()["transfers"][0]
            if final["status"] == "failed":
                break
            time.sleep(0.01)
        assert calls == 1
        assert final["status"] == "failed"
        assert "simulated remote disk full" in final["error"]

        detail = client.get(
            f"/api/sessions/{session['id']}?message_limit=100"
        ).json()["session"]
        transfer_results = [
            message
            for message in detail["messages"]
            if message["type"] == "artifact_upload"
        ]
        assert len(transfer_results) == 1
        failed_artifact = transfer_results[0]["payload"]["artifact"]
        assert failed_artifact["status"] == "failed"
        assert "simulated remote disk full" in failed_artifact["error"]
        assert not failed_artifact.get("sha256")
        assert detail["pending"].get("file_transfer") is None

        from shell_agent.web.runtime import get_runtime
        from shell_agent.web.ws.session_state import _get_hydrated_session_context

        runtime = get_runtime()
        assert runtime.session_contexts[session["id"]].latest_artifact() is None
        runtime.session_contexts.pop(session["id"], None)
        restored_context = asyncio.run(
            _get_hydrated_session_context(runtime, session["id"])
        )
        assert restored_context.latest_artifact() is None
