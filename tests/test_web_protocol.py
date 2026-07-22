"""Behavior-level contract tests for the /ws/chat JSON protocol."""

from types import SimpleNamespace
import re

import pytest
from starlette.websockets import WebSocketDisconnect

from shell_agent.safety.classifier import RiskAssessment, RiskLevel
from shell_agent.core.models import PendingCommand
from shell_agent.web import api
from shell_agent.web.ws.transport import ConnectionManager


class CaptureWebSocket:
    def __init__(self, inbound: dict | None = None) -> None:
        self.inbound = inbound
        self.accepted = False
        self.sent: list[dict] = []
        self._received = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        if not self._received and self.inbound is not None:
            self._received = True
            return self.inbound
        raise WebSocketDisconnect()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class ClosedCaptureWebSocket(CaptureWebSocket):
    async def send_json(self, payload: dict) -> None:
        raise RuntimeError("connection closed")


@pytest.mark.asyncio
async def test_session_broadcast_survives_originating_socket_refresh() -> None:
    manager = ConnectionManager()
    old_socket = ClosedCaptureWebSocket()
    refreshed_socket = CaptureWebSocket()
    await manager.connect(old_socket)
    await manager.connect(refreshed_socket)
    manager.subscribe(refreshed_socket, "sess-refresh")

    await manager.send(
        {
            "type": "turn_state",
            "session_id": "sess-refresh",
            "turn_id": "task-refresh",
            "status": "completed",
        },
        preferred=old_socket,
    )

    assert refreshed_socket.sent == [{
        "type": "turn_state",
        "session_id": "sess-refresh",
        "turn_id": "task-refresh",
        "status": "completed",
    }]


@pytest.mark.asyncio
async def test_outbound_message_envelope_preserves_session_and_turn_context() -> None:
    websocket = CaptureWebSocket()
    session_token = api._SEND_SESSION_ID.set("sess_contract")
    turn_token = api._SEND_TURN_ID.set("turn_contract")
    try:
        await api._send(websocket, "system", content="ready", channel="chat")
    finally:
        api._SEND_TURN_ID.reset(turn_token)
        api._SEND_SESSION_ID.reset(session_token)

    assert len(websocket.sent) == 1
    message = websocket.sent[0]
    assert set(message) == {
        "type",
        "timestamp",
        "session_id",
        "turn_id",
        "content",
        "channel",
    }
    assert message | {"timestamp": "<time>"} == {
        "type": "system",
        "timestamp": "<time>",
        "session_id": "sess_contract",
        "turn_id": "turn_contract",
        "content": "ready",
        "channel": "chat",
    }
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", message["timestamp"])


def test_command_preview_payload_field_contract() -> None:
    runtime = SimpleNamespace(session_contexts={})
    command = PendingCommand(
        raw="ssh prod-01 'rm app.log'",
        target="prod-01",
        target_env="prod",
        executor="ssh",
        actual_command="rm app.log",
        intent="清理日志",
        explanation="释放空间",
        confirm_mode="interactive",
        policy_blocked=False,
        requires_secondary_confirm=True,
        secondary_confirm_expected="prod-01",
        secondary_confirm_label="目标别名",
        secondary_confirm_reason="生产环境危险操作",
        task_id="task_contract",
    )
    risk = RiskAssessment(
        level=RiskLevel.DANGEROUS,
        reasons=["删除文件会改变目标机器状态"],
        rules=["rm_delete"],
    )

    payload = api._command_preview_payload(
        runtime,
        "sess_contract",
        command,
        "chat",
        risk=risk,
    )

    assert set(payload) == {
        "session_id",
        "task_id",
        "operation_id",
        "turn_id",
        "channel",
        "step_index",
        "total_steps",
        "command",
        "target",
        "cwd",
        "intent",
        "explanation",
        "skill_name",
        "skill_version",
        "skill_hash",
        "skill_step_name",
        "confirm_mode",
        "policy_blocked",
        "policy_block_reason",
        "requires_secondary_confirm",
        "secondary_confirm_expected",
        "secondary_confirm_label",
        "secondary_confirm_reason",
        "risk_level",
        "risk_reasons",
        "risk_rules",
    }
    assert payload["session_id"] == "sess_contract"
    assert payload["task_id"] == payload["turn_id"] == "task_contract"
    assert payload["operation_id"] == command.id
    assert payload["channel"] == "chat"
    assert payload["risk_level"] == "dangerous"
    assert payload["requires_secondary_confirm"] is True


@pytest.mark.asyncio
async def test_completion_result_field_contract() -> None:
    websocket = CaptureWebSocket()
    await api._send_completion(
        websocket,
        request_id="req-1",
        input_id="terminal-input",
        token={"start": 3, "end": 5, "prefix": "lo"},
        kind="path",
        candidates=["logs/", "local/"] + [f"item-{index}" for index in range(100)],
    )

    message = websocket.sent[0]
    assert set(message) == {
        "type",
        "timestamp",
        "channel",
        "request_id",
        "input_id",
        "kind",
        "start",
        "end",
        "prefix",
        "candidates",
        "common_prefix",
    }
    assert message["type"] == "completion_result"
    assert message["channel"] == "command"
    assert message["request_id"] == "req-1"
    assert message["input_id"] == "terminal-input"
    assert message["kind"] == "path"
    assert len(message["candidates"]) == 80


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inbound", "handler_name", "expected_args"),
    [
        (
            {"message": "检查磁盘", "session_id": "sess-1"},
            "_start_chat_turn",
            ("sess-1", "检查磁盘", "interactive", ""),
        ),
        (
            {
                "type": "command",
                "session_id": "sess-2",
                "command": "df -h",
                "confirm_mode": "dry_run",
                "target": "dev-01",
                "cwd": "/srv",
            },
            "_handle_direct_command",
            ("sess-2", "df -h", "dry_run", "dev-01", "/srv"),
        ),
        (
            {
                "type": "confirm",
                "session_id": "sess-3",
                "confirmed": True,
                "channel": "command",
                "task_id": "task-3",
                "secondary_confirm_value": "prod-01",
                "operation_id": "op-3",
                "request_id": "req-3",
            },
            "_handle_confirm",
            ("sess-3", True, "command", "task-3", "prod-01", "op-3", "req-3"),
        ),
        (
            {"type": "cancel", "session_id": "sess-4"},
            "_handle_cancel",
            ("sess-4", "command"),
        ),
        (
            {
                "type": "complete",
                "session_id": "sess-5",
                "command": "ls lo",
                "cursor": "5",
                "target": "dev-01",
                "cwd": "/srv",
                "request_id": "req-5",
                "input_id": "input-5",
            },
            "_handle_completion",
            ("sess-5", "ls lo", 5, "dev-01", "/srv", "req-5", "input-5"),
        ),
        (
            {
                "type": "plan_confirm",
                "session_id": "sess-6",
                "plan_id": "plan-6",
                "confirmed": True,
            },
            "_handle_plan_confirm",
            ("sess-6", "plan-6", True),
        ),
        (
            {
                "type": "plan_adjust",
                "session_id": "sess-7",
                "plan_id": "plan-7",
                "instruction": "先备份",
            },
            "_handle_plan_adjust",
            ("sess-7", "plan-7", "先备份"),
        ),
        (
            {"type": "subscribe", "session_id": "sess-8", "channel": "chat"},
            "_send_session_sync",
            ("sess-8", "chat"),
        ),
    ],
)
async def test_inbound_message_dispatch_contract(
    monkeypatch,
    inbound: dict,
    handler_name: str,
    expected_args: tuple,
) -> None:
    runtime = object()
    calls: list[tuple[str, tuple]] = []

    def chat_handler(_websocket, _runtime, *args) -> None:
        calls.append(("_start_chat_turn", args))

    async def async_handler(name: str, _websocket, _runtime, *args) -> None:
        calls.append((name, args))

    monkeypatch.setattr(api, "get_runtime", lambda: runtime)
    monkeypatch.setattr(api, "_start_chat_turn", chat_handler)
    for name in (
        "_handle_confirm",
        "_handle_plan_confirm",
        "_handle_plan_adjust",
        "_handle_direct_command",
        "_handle_completion",
        "_handle_cancel",
        "_send_session_sync",
    ):
        async def replacement(_websocket, _runtime, *args, _name=name):
            await async_handler(_name, _websocket, _runtime, *args)

        monkeypatch.setattr(api, name, replacement)

    websocket = CaptureWebSocket(inbound)
    await api.chat_ws(websocket)

    assert websocket.accepted is True
    assert calls == [(handler_name, expected_args)]
