"""Shared WebSocket transport state and JSON delivery helpers."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime

from fastapi import WebSocket
from loguru import logger


_SEND_SESSION_ID: ContextVar[str] = ContextVar(
    "shell_agent_send_session_id",
    default="",
)
_SEND_TURN_ID: ContextVar[str] = ContextVar(
    "shell_agent_send_turn_id",
    default="",
)


class ConnectionManager:
    """Track active WebSocket connections."""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []
        self.session_subscriptions: dict[WebSocket, set[str]] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        self.session_subscriptions.setdefault(ws, set())

    def subscribe(self, ws: WebSocket, session_id: str) -> None:
        """Attach one connection to a persisted session's live event stream."""
        if session_id:
            self.session_subscriptions.setdefault(ws, set()).add(session_id)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)
        self.session_subscriptions.pop(ws, None)

    async def send(
        self,
        payload: dict,
        *,
        preferred: WebSocket | None = None,
        broadcast: bool = True,
    ) -> None:
        """Deliver to the originating socket and any refreshed session viewers.

        Background work outlives the browser WebSocket that started it.  A
        refreshed page subscribes its new connection to the same session, so
        persisted task progress continues to arrive without another refresh.
        """
        targets: list[WebSocket] = []
        if preferred is not None:
            targets.append(preferred)
        session_id = str(payload.get("session_id") or "")
        if broadcast and session_id:
            targets.extend(
                ws
                for ws, sessions in self.session_subscriptions.items()
                if session_id in sessions
            )

        delivered: set[int] = set()
        for ws in targets:
            identity = id(ws)
            if identity in delivered:
                continue
            delivered.add(identity)
            try:
                await ws.send_json(payload)
            except Exception as exc:
                logger.debug(
                    f"WebSocket 推送失败，事件已忽略: "
                    f"type={payload.get('type')} error={exc}"
                )


manager = ConnectionManager()


async def _send(
    websocket: WebSocket,
    msg_type: str,
    *,
    broadcast: bool = True,
    **kwargs,
) -> None:
    """Send one protocol event, enriching it with current session/turn IDs."""
    payload = {"type": msg_type, "timestamp": datetime.now().strftime("%H:%M:%S")}
    payload.update(kwargs)
    session_id = payload.get("session_id") or _SEND_SESSION_ID.get()
    if session_id:
        payload["session_id"] = session_id
    turn_id = payload.get("turn_id") or _SEND_TURN_ID.get()
    if turn_id:
        payload["turn_id"] = turn_id
    await manager.send(payload, preferred=websocket, broadcast=broadcast)
