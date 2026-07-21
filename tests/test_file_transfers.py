from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from shell_agent.executors.ssh import (
    SSHExecutor,
    VerifiedUploadResult,
    normalize_upload_destination,
)
from shell_agent.storage.database import connect, init_db
from shell_agent.storage.file_transfers import (
    claim_file_transfer,
    confirm_file_transfer,
    create_file_transfer,
    get_file_transfer,
    has_active_file_transfer,
    interrupt_running_file_transfers,
)
from shell_agent.storage.session_files import create_session_file
from shell_agent.storage.sessions import ensure_session
from shell_agent.storage.tasks import create_task, reconcile_orphaned_tasks, update_task
from shell_agent.utils.config import ServerEntry
from shell_agent.web.app import create_app


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
    (config / "credentials.yaml").write_text("credentials: []\n", encoding="utf-8")
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


@pytest.mark.asyncio
async def test_file_transfer_storage_is_idempotent_and_claim_is_atomic(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await ensure_session(db, "sess_a", session_type="chat")
        source = tmp_path / "app.jar"
        source.write_bytes(b"jar")
        file = await create_session_file(
            db,
            session_id="sess_a",
            original_name="app.jar",
            stored_path=str(source),
            media_type="application/java-archive",
            extension=".jar",
            size=3,
            sha256="0163f1eea7894c32204a9a2b2e3c8d402d0f14d5c7a634a3ef384c7da93b929c",
        )
        values = dict(
            request_id="req-1",
            session_id="sess_a",
            file_id=file["id"],
            file_name="app.jar",
            target="dev-01",
            remote_dir="/tmp/shell-agent-uploads",
            remote_name="app.jar",
            remote_path="/tmp/shell-agent-uploads/app.jar",
            overwrite=False,
            size=3,
            sha256=file["sha256"],
        )
        first, first_created = await create_file_transfer(db, **values)
        second, second_created = await create_file_transfer(db, **values)

        assert first_created is True
        assert second_created is False
        assert second["id"] == first["id"]

        claims = await asyncio.gather(
            claim_file_transfer(db, first["id"], "sess_a"),
            claim_file_transfer(db, first["id"], "sess_a"),
        )
        assert sorted(claimed for claimed, _ in claims) == [False, True]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_file_transfer_startup_reconciles_pending_and_running(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await ensure_session(db, "sess_a", session_type="chat")
        source = tmp_path / "app.jar"
        source.write_bytes(b"jar")
        file = await create_session_file(
            db,
            session_id="sess_a",
            original_name="app.jar",
            stored_path=str(source),
            media_type="application/java-archive",
            extension=".jar",
            size=3,
            sha256="hash",
        )
        transfers = []
        for request_id in ("pending", "running"):
            transfer, _ = await create_file_transfer(
                db,
                request_id=request_id,
                session_id="sess_a",
                file_id=file["id"],
                file_name="app.jar",
                target="dev-01",
                remote_dir="/tmp",
                remote_name=f"{request_id}.jar",
                remote_path=f"/tmp/{request_id}.jar",
                overwrite=False,
                size=3,
                sha256="hash",
            )
            transfers.append(transfer)
        await claim_file_transfer(db, transfers[1]["id"], "sess_a")

        interrupted = await interrupt_running_file_transfers(db)

        assert set(interrupted) == {item["id"] for item in transfers}
        for item in transfers:
            restored = await get_file_transfer(db, item["id"])
            assert restored and restored["status"] == "interrupted"
            assert restored["completed_at"]
    finally:
        await db.close()


def test_upload_destination_rejects_traversal_and_controls() -> None:
    assert normalize_upload_destination("/tmp/uploads", "app.jar") == (
        "/tmp/uploads", "app.jar", "/tmp/uploads/app.jar"
    )
    with pytest.raises(ValueError):
        normalize_upload_destination("tmp/uploads", "app.jar")
    with pytest.raises(ValueError):
        normalize_upload_destination("/tmp/../root", "app.jar")
    with pytest.raises(ValueError):
        normalize_upload_destination("/tmp/uploads", "../app.jar")
    with pytest.raises(ValueError):
        normalize_upload_destination("/tmp/uploads\n", "app.jar")


class _RemoteReader:
    def __init__(self, content: bytes):
        self.content = content
        self.offset = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self, size: int) -> bytes:
        value = self.content[self.offset:self.offset + size]
        self.offset += len(value)
        return value


class _FakeSFTP:
    def __init__(self, *, corrupt: bool = False):
        self.files: dict[str, bytes] = {}
        self.corrupt = corrupt

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def makedirs(self, _path: str, exist_ok: bool = False):
        assert exist_ok is True

    async def put(self, local: str, remote: str, **_kwargs):
        data = Path(local).read_bytes()
        self.files[remote] = data + (b"corrupt" if self.corrupt else b"")

    async def stat(self, path: str):
        return SimpleNamespace(size=len(self.files[path]))

    def open(self, path: str, _mode: str):
        return _RemoteReader(self.files[path])

    async def rename(self, old: str, new: str):
        if new in self.files:
            raise OSError("destination exists")
        self.files[new] = self.files.pop(old)

    async def posix_rename(self, old: str, new: str):
        self.files[new] = self.files.pop(old)

    async def remove(self, path: str):
        if path not in self.files:
            raise OSError("not found")
        del self.files[path]


class _FakeConnection:
    def __init__(self, sftp: _FakeSFTP):
        self.sftp = sftp

    def start_sftp_client(self):
        return self.sftp

    async def run(self, _command: str, check: bool = False):
        # Force the portable SFTP-read hash fallback.
        return SimpleNamespace(exit_status=127, stdout="", stderr="missing")


@pytest.mark.asyncio
async def test_executor_verified_upload_is_atomic_and_cleans_bad_part(tmp_path, monkeypatch) -> None:
    source = tmp_path / "app.jar"
    source.write_bytes(b"verified-content")
    executor = SSHExecutor(
        servers={
            "dev-01": ServerEntry(
                alias="dev-01", host="127.0.0.1", ssh_credential="unused"
            )
        },
        credentials={},
    )
    sftp = _FakeSFTP()
    connection = _FakeConnection(sftp)

    async def get_connection(_target: str):
        return connection

    async def release_connection(_target: str, _connection):
        return None

    monkeypatch.setattr(executor, "_get_connection", get_connection)
    monkeypatch.setattr(executor, "_release_connection", release_connection)

    result = await executor.upload_file_verified(
        target="dev-01",
        local_path=source,
        remote_dir="/tmp/uploads",
        remote_name="app.jar",
        operation_id="transfer-1",
    )

    assert result.remote_path == "/tmp/uploads/app.jar"
    assert sftp.files == {"/tmp/uploads/app.jar": b"verified-content"}

    corrupt = _FakeSFTP(corrupt=True)
    connection.sftp = corrupt
    with pytest.raises(IOError, match="大小校验失败"):
        await executor.upload_file_verified(
            target="dev-01",
            local_path=source,
            remote_dir="/tmp/uploads",
            remote_name="bad.jar",
            operation_id="transfer-2",
        )
    assert corrupt.files == {}


def test_session_file_transfer_api_is_scoped_idempotent_and_never_connects(
    tmp_path, monkeypatch
) -> None:
    config_path = _runtime_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("test must not open a real SSH connection")

    calls = 0

    async def fake_upload(self, **kwargs):
        nonlocal calls
        calls += 1
        path = Path(kwargs["local_path"])
        return VerifiedUploadResult(
            remote_path=f"{kwargs['remote_dir']}/{kwargs['remote_name']}",
            size=path.stat().st_size,
            sha256=kwargs["expected_sha256"],
        )

    monkeypatch.setattr("shell_agent.executors.ssh.asyncssh.connect", forbidden_connect)
    monkeypatch.setattr(SSHExecutor, "upload_file_verified", fake_upload)

    with TestClient(create_app(str(config_path))) as client:
        first_session = client.post(
            "/api/sessions", json={"type": "chat", "title": "transfer"}
        ).json()["session"]
        second_session = client.post(
            "/api/sessions", json={"type": "chat", "title": "other"}
        ).json()["session"]
        uploaded = client.post(
            f"/api/sessions/{first_session['id']}/files",
            files={"files": ("app.jar", b"fake-jar", "application/java-archive")},
        ).json()["files"][0]
        body = {
            "target": "dev-01",
            "remote_dir": "/tmp/shell-agent-uploads",
            "remote_name": "app.jar",
            "overwrite": False,
            "request_id": "request-123",
        }

        cross_session = client.post(
            f"/api/sessions/{second_session['id']}/files/{uploaded['id']}/transfers",
            json=body,
        )
        assert cross_session.status_code == 404

        first = client.post(
            f"/api/sessions/{first_session['id']}/files/{uploaded['id']}/transfers",
            json=body,
        )
        duplicate = client.post(
            f"/api/sessions/{first_session['id']}/files/{uploaded['id']}/transfers",
            json=body,
        )
        assert first.status_code == 202
        assert duplicate.status_code == 202
        assert first.json()["ok"] is True
        assert first.json()["transfer"]["id"] == duplicate.json()["transfer"]["id"]
        assert "stored_path" not in str(first.json())
        assert "local_cache_path" not in str(first.json())

        transfer = first.json()["transfer"]
        for _ in range(100):
            response = client.get(
                f"/api/sessions/{first_session['id']}/file-transfers"
            )
            transfer = response.json()["transfers"][0]
            if transfer["status"] == "success":
                break
            time.sleep(0.01)

        assert transfer["status"] == "success"
        assert transfer["filename"] == "app.jar"
        assert transfer["remote_sha256"] == uploaded["sha256"]
        assert calls == 1

        audit = client.get("/api/audit?target=dev-01").json()["records"]
        assert any(record["executor"] == "sftp" and record["exit_code"] == 0 for record in audit)
        detail = client.get(
            f"/api/sessions/{first_session['id']}?message_limit=50"
        ).json()["session"]
        artifact_messages = [
            message for message in detail["messages"]
            if message["type"] == "artifact_upload"
        ]
        assert artifact_messages
        assert "local_cache_path" not in str(artifact_messages)


def _receive_ws_type(websocket, expected: str, *, request_id: str = "") -> dict:
    for _ in range(40):
        message = websocket.receive_json()
        if message.get("type") != expected:
            continue
        if request_id and message.get("request_id") != request_id:
            continue
        return message
    raise AssertionError(f"WebSocket 未收到事件: {expected}")


def test_conversational_transfer_waits_for_confirmation_and_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    config_path = _runtime_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = 0

    async def fake_upload(self, **kwargs):
        nonlocal calls
        calls += 1
        path = Path(kwargs["local_path"])
        return VerifiedUploadResult(
            remote_path=f"{kwargs['remote_dir']}/{kwargs['remote_name']}",
            size=path.stat().st_size,
            sha256=kwargs["expected_sha256"],
        )

    monkeypatch.setattr(SSHExecutor, "upload_file_verified", fake_upload)

    with TestClient(create_app(str(config_path))) as client:
        session = client.post(
            "/api/sessions", json={"type": "chat", "title": "chat upload"}
        ).json()["session"]
        uploaded = client.post(
            f"/api/sessions/{session['id']}/files",
            files={"files": ("app.jar", b"fake-jar", "application/java-archive")},
        ).json()["files"][0]

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({
                "type": "chat",
                "session_id": session["id"],
                "message": "把 app.jar 上传到 dev-01 的 /tmp/releases",
                # Even full access must not bypass this remote write confirmation.
                "confirm_mode": "full_access",
            })
            preview = _receive_ws_type(websocket, "file_transfer_preview")
            transfer = preview["transfer"]
            assert transfer["file_id"] == uploaded["id"]
            assert transfer["status"] == "waiting_confirm"
            assert transfer["target"] == "dev-01"
            assert transfer["target_env"] == "dev"
            assert transfer["remote_path"] == "/tmp/releases/app.jar"
            assert transfer["overwrite"] is False
            assert preview["requires_confirmation"] is True
            assert calls == 0

            websocket.send_json({
                "type": "subscribe",
                "session_id": session["id"],
                "channel": "chat",
            })
            sync = _receive_ws_type(websocket, "session_sync")
            assert sync["pending"]["file_transfer"]["id"] == transfer["id"]
            waiting_tasks = [
                task for task in sync["tasks"] if task["status"] == "waiting_confirm"
            ]
            assert waiting_tasks

            websocket.send_json({
                "type": "file_transfer_confirm",
                "session_id": session["id"],
                "transfer_id": transfer["id"],
                "confirmed": True,
                "request_id": "confirm-1",
            })
            first_ack = _receive_ws_type(
                websocket, "file_transfer_confirm_ack", request_id="confirm-1"
            )
            assert first_ack["accepted"] is True
            assert first_ack["duplicate"] is False

            websocket.send_json({
                "type": "file_transfer_confirm",
                "session_id": session["id"],
                "transfer_id": transfer["id"],
                "confirmed": True,
                "request_id": "confirm-2",
            })
            second_ack = _receive_ws_type(
                websocket, "file_transfer_confirm_ack", request_id="confirm-2"
            )
            assert second_ack["accepted"] is True
            assert second_ack["duplicate"] is True

        final = transfer
        for _ in range(100):
            final = client.get(
                f"/api/sessions/{session['id']}/file-transfers"
            ).json()["transfers"][0]
            if final["status"] == "success":
                break
            time.sleep(0.01)
        assert final["status"] == "success"
        assert calls == 1

        audit = client.get("/api/audit?target=dev-01").json()["records"]
        conversational_audit = next(
            record for record in audit
            if record["executor"] == "sftp" and record["exit_code"] == 0
        )
        assert conversational_audit["source"] == "chat"

        detail = client.get(
            f"/api/sessions/{session['id']}?message_limit=100"
        ).json()["session"]
        assert detail["pending"].get("file_transfer") is None
        assert any(
            message["type"] == "file_transfer_preview"
            for message in detail["messages"]
        )
        assert any(
            message["type"] == "artifact_upload"
            for message in detail["messages"]
        )


def test_conversational_transfer_reject_is_durable_and_opposite_decision_conflicts(
    tmp_path, monkeypatch
) -> None:
    config_path = _runtime_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = 0

    async def fake_upload(self, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("rejected transfer must not upload")

    monkeypatch.setattr(SSHExecutor, "upload_file_verified", fake_upload)

    with TestClient(create_app(str(config_path))) as client:
        session = client.post(
            "/api/sessions", json={"type": "chat", "title": "reject upload"}
        ).json()["session"]
        uploaded = client.post(
            f"/api/sessions/{session['id']}/files",
            files={"files": ("app.jar", b"fake-jar", "application/java-archive")},
        ).json()["files"][0]
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({
                "type": "chat",
                "session_id": session["id"],
                "message": "把 app.jar 传到 dev-01 /tmp/releases",
            })
            transfer = _receive_ws_type(websocket, "file_transfer_preview")["transfer"]

        assert client.delete(f"/api/session-files/{uploaded['id']}").status_code == 409
        assert client.delete(f"/api/sessions/{session['id']}").status_code == 409

        url = f"/api/sessions/{session['id']}/file-transfers/{transfer['id']}/confirm"
        rejected = client.post(url, json={"confirmed": False, "request_id": "reject-1"})
        repeated = client.post(url, json={"confirmed": False, "request_id": "reject-2"})
        opposite = client.post(url, json={"confirmed": True, "request_id": "confirm-late"})
        assert rejected.status_code == 202
        assert rejected.json()["duplicate"] is False
        assert rejected.json()["transfer"]["status"] == "cancelled"
        assert repeated.status_code == 202
        assert repeated.json()["duplicate"] is True
        assert opposite.status_code == 409
        assert calls == 0
        assert client.delete(f"/api/session-files/{uploaded['id']}").status_code == 200


def test_conversational_transfer_recent_file_is_deterministic_and_deploy_is_not_intercepted(
    tmp_path, monkeypatch
) -> None:
    config_path = _runtime_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with TestClient(create_app(str(config_path))) as client:
        session = client.post(
            "/api/sessions", json={"type": "chat", "title": "latest file"}
        ).json()["session"]
        client.post(
            f"/api/sessions/{session['id']}/files",
            files={"files": ("old.jar", b"old", "application/java-archive")},
        )
        newest = client.post(
            f"/api/sessions/{session['id']}/files",
            files={"files": ("new.jar", b"new", "application/java-archive")},
        ).json()["files"][0]

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({
                "type": "chat",
                "session_id": session["id"],
                "message": "把刚上传的传到 dev-01 /tmp/releases",
            })
            preview = _receive_ws_type(websocket, "file_transfer_preview")
            assert preview["transfer"]["file_id"] == newest["id"]
            assert preview["transfer"]["filename"] == "new.jar"

        # Resolve the waiting preview before starting a second chat turn.
        transfer_id = preview["transfer"]["id"]
        client.post(
            f"/api/sessions/{session['id']}/file-transfers/{transfer_id}/confirm",
            json={"confirmed": False},
        )

        from shell_agent.web.runtime import get_runtime
        from shell_agent.web.ws.file_transfer_intent import (
            resolve_conversational_file_transfer,
        )

        resolution = asyncio.run(resolve_conversational_file_transfer(
            get_runtime(),
            session["id"],
            "把刚上传的文件部署到 dev-01 /data/app",
        ))
        assert resolution.attempted is False


def test_conversational_transfer_refuses_ambiguous_inputs_and_changed_target(
    tmp_path, monkeypatch
) -> None:
    config_path = _runtime_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = 0

    async def fake_upload(self, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("changed target must not upload")

    monkeypatch.setattr(SSHExecutor, "upload_file_verified", fake_upload)

    with TestClient(create_app(str(config_path))) as client:
        session = client.post(
            "/api/sessions", json={"type": "chat", "title": "target snapshot"}
        ).json()["session"]
        client.post(
            f"/api/sessions/{session['id']}/files",
            files={"files": ("one.jar", b"one", "application/java-archive")},
        )
        client.post(
            f"/api/sessions/{session['id']}/files",
            files={"files": ("two.jar", b"two", "application/java-archive")},
        )

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({
                "type": "chat",
                "session_id": session["id"],
                "message": "把文件上传到 dev-01 /tmp/releases",
            })
            clarification = _receive_ws_type(websocket, "agent")
            assert "多个文件" in clarification["content"]
        assert client.get(
            f"/api/sessions/{session['id']}/file-transfers"
        ).json()["transfers"] == []

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({
                "type": "chat",
                "session_id": session["id"],
                "message": "把 two.jar 上传到 dev-01 /tmp/releases",
            })
            transfer = _receive_ws_type(websocket, "file_transfer_preview")["transfer"]
        assert "target_fingerprint" not in transfer

        from shell_agent.web.runtime import get_runtime

        get_runtime().executor.servers["dev-01"].host = "127.0.0.2"
        confirm = client.post(
            f"/api/sessions/{session['id']}/file-transfers/{transfer['id']}/confirm",
            json={"confirmed": True},
        )
        assert confirm.status_code == 409
        assert "配置已变化" in confirm.json()["detail"]
        assert calls == 0
        still_waiting = client.get(
            f"/api/sessions/{session['id']}/file-transfers"
        ).json()["transfers"][0]
        assert still_waiting["status"] == "waiting_confirm"

        # Rejecting remains possible even after the target config changes.
        rejected = client.post(
            f"/api/sessions/{session['id']}/file-transfers/{transfer['id']}/confirm",
            json={"confirmed": False},
        )
        assert rejected.status_code == 202
        assert rejected.json()["transfer"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_waiting_conversational_transfer_survives_restart_reconciliation(
    tmp_path,
) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        await ensure_session(db, "sess_wait", session_type="chat")
        source = tmp_path / "app.jar"
        source.write_bytes(b"jar")
        file = await create_session_file(
            db,
            session_id="sess_wait",
            original_name="app.jar",
            stored_path=str(source),
            media_type="application/java-archive",
            extension=".jar",
            size=3,
            sha256="0163f1eea7894c32204a9a2b2e3c8d402d0f14d5c7a634a3ef384c7da93b929c",
        )
        task = await create_task(db, "sess_wait", "chat", title="upload")
        await update_task(db, task["id"], status="waiting_confirm")
        transfer, _ = await create_file_transfer(
            db,
            request_id="chat-restart",
            session_id="sess_wait",
            file_id=file["id"],
            file_name="app.jar",
            target="dev-01",
            target_env="dev",
            remote_dir="/tmp/releases",
            remote_name="app.jar",
            remote_path="/tmp/releases/app.jar",
            overwrite=False,
            size=3,
            sha256=file["sha256"],
            initial_status="waiting_confirm",
            source="chat",
            turn_id=task["id"],
        )

        assert await reconcile_orphaned_tasks(db) == []
        assert await interrupt_running_file_transfers(db) == []
        restored = await get_file_transfer(db, transfer["id"])
        assert restored and restored["status"] == "waiting_confirm"
        assert await has_active_file_transfer(db, file["id"]) is True

        first, first_record = await confirm_file_transfer(
            db, transfer["id"], "sess_wait", confirmed=False
        )
        second, second_record = await confirm_file_transfer(
            db, transfer["id"], "sess_wait", confirmed=False
        )
        assert first is True and first_record["status"] == "cancelled"
        assert second is False and second_record["status"] == "cancelled"
    finally:
        await db.close()
