"""Deterministic browser-test server for the Shell Agent workbench.

It serves the production Vue build and implements only the HTTP/WebSocket
contract needed by the E2E tests. It never creates an SSH executor or LLM.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import posixpath
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


FRONTEND_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = FRONTEND_ROOT.parent / "static"
BUILD_ROOT = STATIC_ROOT / "next"
SESSION_ID = "e2e-session"
TASK_ID = "e2e-task"
FILE_ID = "e2e-file"
FILE_NAME = "bedcare-mock.jar"
FILE_SIZE = 18_245_632
FILE_SHA256 = "9f74b3a3125b91787ef80d8a26823d278bfb2c86d3cb3aefa6f3fbc09dd63f4a"
DEPLOYMENT_RUN_ID = "e2e-deployment-run"
DEPLOYMENT_PLAN_HASH = "e2e-frozen-plan-sha256"
CONVERSATION_TRANSFER_ID = "e2e-conversation-transfer"
CONVERSATION_REMOTE_PATH = "/tmp/shell-agent-uploads/bedcare-mock.jar"

app = FastAPI(title="Shell Agent deterministic E2E server")
state: dict[str, Any] = {
    "scenario": "waiting_confirm",
    "confirm_count": 0,
    "transfer_post_count": 0,
    "transfer_execution_count": 0,
    "conversation_transfer_confirm_count": 0,
    "conversation_transfer_execution_count": 0,
    "conversation_transfer": None,
    "transfers": {},
    "transfer_request_ids": {},
    "deployment_create_count": 0,
    "deployment_confirm_count": 0,
    "deployment_execution_count": 0,
    "deployment_rollback_count": 0,
    "deployment_run": None,
}
session_subscribers: set[WebSocket] = set()


def _session_file() -> dict[str, Any]:
    return {
        "id": FILE_ID,
        "session_id": SESSION_ID,
        "name": FILE_NAME,
        "media_type": "application/java-archive",
        "extension": ".jar",
        "kind": "archive",
        "preview_type": "none",
        "size": FILE_SIZE,
        "sha256": FILE_SHA256,
        "parse_status": "ready",
        "parse_error": "",
        "metadata": {},
        "created_at": "2026-07-16T09:00:00",
        "preview_url": f"/api/session-files/{FILE_ID}/preview",
        "content_url": f"/api/session-files/{FILE_ID}/content",
        "download_url": f"/api/session-files/{FILE_ID}/download",
    }


def _server() -> dict[str, Any]:
    return {
        "alias": "fake-host",
        "host": "192.0.2.10",
        "port": 22,
        "env": "test",
        "role": "e2e-fixture",
        "ssh_credential": "fake-credential",
        "ssh_username": "e2e-user",
        "ssh_auth_type": "key",
        "ssh_password_set": False,
        "ssh_private_key_set": True,
        "ssh_passphrase_set": False,
        "tags": ["isolated", "no-network"],
    }


def _service() -> dict[str, Any]:
    """A verified test-only service profile; it never resolves to real SSH."""
    return {
        "id": "bedcare-mock",
        "name": "bedcare-mock",
        "env": "test",
        "owners": ["e2e"],
        "servers": ["fake-host"],
        "deploy_dir": "/srv/bedcare-mock",
        "artifact_path": "/srv/bedcare-mock/lib/bedcare-mock.jar",
        "backup_dir": "/srv/bedcare-mock/.shell-agent/backups",
        "artifact_type": "jar",
        "startup_timeout_seconds": 30,
        "log_dir": "/srv/bedcare-mock/logs",
        "health_url": "http://127.0.0.1:18091/health",
        "ports": [18091],
        "start_cmd": "systemctl start bedcare-mock",
        "stop_cmd": "systemctl stop bedcare-mock",
        "restart_cmd": "systemctl restart bedcare-mock",
        "status_cmd": "systemctl is-active bedcare-mock",
        "config_paths": [],
        "runtime": "java",
        "version": "0.1.0",
        "last_verified_at": "2026-07-16T08:55:00",
        "verification_status": "verified",
        "source_task_id": "e2e-fixture",
        "revision": 3,
        "tags": ["isolated", "e2e"],
        "notes": "Deterministic browser fixture; no SSH executor exists.",
    }


def _deployment_plan_steps() -> list[dict[str, Any]]:
    return [
        {"id": "precheck_host", "name": "检查目标与部署目录", "phase": "precheck", "action": "precheck_host", "risk_level": "safe", "mutates_live": False},
        {"id": "precheck_artifact", "name": "校验会话制品", "phase": "precheck", "action": "precheck_artifact", "risk_level": "safe", "mutates_live": False},
        {"id": "stage_upload", "name": "上传制品到暂存目录", "phase": "execute", "action": "stage_upload", "risk_level": "caution", "mutates_live": False},
        {"id": "verify_staged", "name": "核对远端 SHA-256", "phase": "execute", "action": "verify_staged", "risk_level": "safe", "mutates_live": False},
        {"id": "backup_current", "name": "备份当前制品", "phase": "execute", "action": "backup_current", "risk_level": "caution", "mutates_live": False},
        {"id": "stop_service", "name": "停止服务", "phase": "execute", "action": "stop_service", "risk_level": "dangerous", "mutates_live": True},
        {"id": "switch_artifact", "name": "原子替换制品", "phase": "execute", "action": "switch_artifact", "risk_level": "dangerous", "mutates_live": True},
        {"id": "start_service", "name": "启动服务", "phase": "execute", "action": "start_service", "risk_level": "dangerous", "mutates_live": True},
        {"id": "postcheck_service", "name": "验证进程、端口与健康检查", "phase": "postcheck", "action": "postcheck_service", "risk_level": "safe", "mutates_live": False},
        {"id": "postcheck_artifact", "name": "确认在线制品校验值", "phase": "postcheck", "action": "postcheck_artifact", "risk_level": "safe", "mutates_live": False},
        {"id": "rollback_stop", "name": "停止异常服务", "phase": "rollback", "action": "rollback_stop", "risk_level": "dangerous", "mutates_live": True},
        {"id": "rollback_restore", "name": "恢复部署前制品", "phase": "rollback", "action": "rollback_restore", "risk_level": "dangerous", "mutates_live": True},
        {"id": "rollback_start", "name": "启动恢复版本", "phase": "rollback", "action": "rollback_start", "risk_level": "dangerous", "mutates_live": True},
        {"id": "rollback_postcheck", "name": "验证恢复后的服务", "phase": "rollback_postcheck", "action": "rollback_postcheck", "risk_level": "safe", "mutates_live": False},
    ]


def _deployment_step_records(status: str) -> list[dict[str, Any]]:
    definitions = _deployment_plan_steps()
    prechecks = {"precheck_host", "precheck_artifact"}
    completed_before_failure = {
        "precheck_host", "precheck_artifact", "stage_upload", "verify_staged",
        "backup_current", "stop_service", "switch_artifact",
    }
    records: list[dict[str, Any]] = []
    for index, definition in enumerate(definitions):
        step_status = "pending"
        if definition["id"] in prechecks:
            step_status = "success"
        if status in {"staging_upload", "completed"} and definition["id"] == "stage_upload":
            step_status = "running" if status == "staging_upload" else "success"
        if status == "completed" and definition["phase"] not in {"rollback", "rollback_postcheck"}:
            step_status = "success"
        if status in {"rollback_required", "rollback_running"}:
            if definition["id"] in completed_before_failure:
                step_status = "success"
            elif definition["id"] == "start_service":
                step_status = "failed"
            elif status == "rollback_running" and definition["id"] == "rollback_stop":
                step_status = "running"
        if status == "unknown" and definition["id"] == "stage_upload":
            step_status = "unknown"
        records.append({
            **definition,
            "step_id": definition["id"],
            "step_index": index,
            "status": step_status,
            "attempt": 1 if step_status != "pending" else 0,
            "exit_code": 0 if step_status == "success" else None,
            "stdout": "fake backend: no remote command executed" if step_status == "success" else "",
            "stderr": "",
            "error": "fake start check failed" if step_status == "failed" else "",
        })
    return records


def _make_deployment_run(status: str) -> dict[str, Any]:
    service = _service()
    plan = {
        "runbook_id": "deploy_single_java_jar",
        "runbook_version": "1.0.0",
        "run_id": DEPLOYMENT_RUN_ID,
        "service": {
            "service_id": service["id"],
            "service_name": service["name"],
            "environment": service["env"],
            "target": service["servers"][0],
            "deploy_dir": service["deploy_dir"],
            "artifact_path": service["artifact_path"],
            "revision": service["revision"],
        },
        "artifact": {
            "file_id": FILE_ID,
            "name": FILE_NAME,
            "size": FILE_SIZE,
            "sha256": FILE_SHA256,
        },
        "steps": _deployment_plan_steps(),
    }
    return {
        "id": DEPLOYMENT_RUN_ID,
        "request_id": "e2e-deployment-request",
        "session_id": SESSION_ID,
        "service_id": service["id"],
        "target": service["servers"][0],
        "environment": service["env"],
        "runbook_id": plan["runbook_id"],
        "runbook_version": plan["runbook_version"],
        "status": status,
        "plan_hash": DEPLOYMENT_PLAN_HASH,
        "confirmed_plan_hash": DEPLOYMENT_PLAN_HASH if status != "waiting_plan_confirm" else None,
        "mutation_started": status in {"rollback_required", "rollback_running", "completed", "unknown"},
        "error": "启动后的健康检查未通过" if status == "rollback_required" else "",
        "result_summary": "制品已替换并通过全部部署后验证。" if status == "completed" else "",
        "created_at": "2026-07-16T09:10:00",
        "updated_at": "2026-07-16T09:10:05",
        "completed_at": "2026-07-16T09:10:05" if status == "completed" else None,
        "plan": plan,
        "steps": _deployment_step_records(status),
        "events": [],
    }


def _transfer_public(transfer: dict[str, Any]) -> dict[str, Any]:
    return dict(transfer)


def _conversation_transfer(status: str = "waiting_confirm") -> dict[str, Any]:
    return {
        "id": CONVERSATION_TRANSFER_ID,
        "request_id": f"chat_{TASK_ID}",
        "session_id": SESSION_ID,
        "file_id": FILE_ID,
        "file_name": FILE_NAME,
        "target": "fake-host",
        "target_env": "test",
        "remote_dir": "/tmp/shell-agent-uploads",
        "remote_name": FILE_NAME,
        "remote_path": CONVERSATION_REMOTE_PATH,
        "overwrite": False,
        "status": status,
        "size": FILE_SIZE,
        "sha256": FILE_SHA256,
        "remote_size": FILE_SIZE if status == "success" else 0,
        "remote_sha256": FILE_SHA256 if status == "success" else "",
        "error": "Permission denied" if status == "failed" else "",
        "source": "chat",
        "turn_id": TASK_ID,
        "created_at": "2026-07-16T09:00:02",
        "updated_at": "2026-07-16T09:00:03",
        "completed_at": "2026-07-16T09:00:04" if status in {"success", "failed", "cancelled"} else "",
    }


def _conversation_transfer_preview(transfer: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": TASK_ID,
        "channel": "chat",
        "transfer": _transfer_public(transfer),
        "requires_confirmation": True,
        "confirm_mode": "interactive",
    }


def _transfers() -> list[dict[str, Any]]:
    values = state.get("transfers") or {}
    transfers = list(values.values())
    conversation_transfer = state.get("conversation_transfer")
    if conversation_transfer:
        transfers.append(conversation_transfer)
    return [
        _transfer_public(transfer)
        for transfer in sorted(transfers, key=lambda item: item["created_at"], reverse=True)
    ]


def _preview_payload() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "turn_id": TASK_ID,
        "channel": "chat",
        "command": "systemctl restart demo-service",
        "target": "fake-host",
        "cwd": "/srv/demo",
        "intent": "重启演示服务",
        "explanation": "确定性 E2E 命令，不会连接真实服务器",
        "confirm_mode": "auto_safe",
        "risk_level": "dangerous",
        "risk_reasons": ["测试场景要求人工确认"],
        "risk_rules": ["e2e_fixture"],
        "policy_blocked": False,
        "policy_block_reason": "",
        "requires_secondary_confirm": False,
        "secondary_confirm_expected": "",
        "secondary_confirm_label": "",
        "secondary_confirm_reason": "",
    }


def _messages() -> list[dict[str, Any]]:
    if str(state.get("scenario") or "").startswith("conversation_transfer"):
        transfer = state.get("conversation_transfer")
        if not transfer:
            return []
        # Persisted previews are immutable audit events. Their payload keeps
        # the original waiting state while the transfer record advances.
        preview = _conversation_transfer_preview({**transfer, "status": "waiting_confirm"})
        messages = [
            {
                "id": "msg-transfer-user",
                "role": "user",
                "type": "user_message",
                "content": f"把 {FILE_NAME} 上传到 fake-host 的 /tmp/shell-agent-uploads",
                "payload": {"turn_id": TASK_ID, "channel": "chat"},
                "created_at": "2026-07-16T09:00:00",
            },
            {
                "id": "msg-transfer-preview",
                "role": "assistant",
                "type": "file_transfer_preview",
                "content": f"准备将 {FILE_NAME} 上传到 fake-host:{CONVERSATION_REMOTE_PATH}",
                "payload": preview,
                "created_at": "2026-07-16T09:00:01",
            },
        ]
        if transfer["status"] in {"success", "failed"}:
            artifact = {
                "id": transfer["id"], "transfer_id": transfer["id"],
                "file_id": FILE_ID, "file_name": FILE_NAME, "target": "fake-host",
                "remote_path": CONVERSATION_REMOTE_PATH, "status": transfer["status"],
                "remote_size": transfer["remote_size"], "remote_sha256": transfer["remote_sha256"],
                "error": transfer["error"],
            }
            messages.append({
                "id": "msg-transfer-result", "role": "system", "type": "artifact_upload",
                "content": transfer["error"] or "文件传输完成", "payload": {"artifact": artifact},
                "created_at": "2026-07-16T09:00:04",
            })
        return messages
    preview = _preview_payload()
    return [
        {
            "id": "msg-user",
            "role": "user",
            "type": "user_message",
            "content": "重启演示服务",
            "payload": {"turn_id": TASK_ID, "channel": "chat"},
            "created_at": "2026-07-16T09:00:00",
        },
        {
            "id": "msg-preview",
            "role": "assistant",
            "type": "command_preview",
            "content": preview["command"],
            "payload": preview,
            "created_at": "2026-07-16T09:00:01",
        },
    ]


def _task(status: str, label: str) -> dict[str, Any]:
    return {
        "id": TASK_ID,
        "session_id": SESSION_ID,
        "channel": "chat",
        "status": status,
        "title": "重启演示服务",
        "current_step": 1,
        "total_steps": 1,
        "confirm_mode": "auto_safe",
        "events": [
            {
                "id": "evt-state",
                "type": "turn_state",
                "status": status,
                "payload": {
                    "turn_id": TASK_ID,
                    "session_id": SESSION_ID,
                    "channel": "chat",
                    "status": status,
                    "label": label,
                    "active": True,
                },
            }
        ],
    }


def _session_detail() -> dict[str, Any]:
    scenario = state["scenario"]
    pending: dict[str, Any] = {}
    tasks: list[dict[str, Any]] = []
    if scenario == "waiting_confirm":
        pending["chat"] = _preview_payload()
        tasks = [_task("waiting_confirm", "等待人工确认")]
    elif scenario == "active":
        tasks = [_task("analyzing", "正在判断是否需要继续下一步")]
    elif scenario == "conversation_transfer_waiting":
        transfer = state.get("conversation_transfer") or _conversation_transfer()
        pending["file_transfer"] = _transfer_public(transfer)
        tasks = [_task("waiting_confirm", "等待确认文件上传")]
    elif scenario == "conversation_transfer_running":
        tasks = [_task("executing", "正在上传文件")]
    return {
        "id": SESSION_ID,
        "type": "chat",
        "title": "E2E 状态恢复会话",
        "created_at": "2026-07-16T09:00:00",
        "updated_at": "2026-07-16T09:00:01",
        "messages": _messages(),
        "pending": pending,
        "tasks": tasks,
    }


@app.get("/__test__/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/__test__/reset")
async def reset(request: Request) -> dict[str, Any]:
    payload = await request.json()
    scenario = str(payload.get("scenario") or "waiting_confirm")
    deployment_scenarios = {
        "deployment_idle": None,
        "deployment_waiting": "waiting_plan_confirm",
        "deployment_running": "staging_upload",
        "deployment_rollback_required": "rollback_required",
        "deployment_unknown": "unknown",
    }
    conversation_scenarios = {
        "conversation_transfer_idle",
        "conversation_transfer_waiting",
        "conversation_transfer_running",
        "conversation_transfer_completed",
        "conversation_transfer_failed",
        "conversation_transfer_rejected",
    }
    if scenario not in {"waiting_confirm", "active", "idle", "file_transfer", *conversation_scenarios, *deployment_scenarios}:
        return JSONResponse({"error": "unknown scenario"}, status_code=400)
    deployment_status = deployment_scenarios.get(scenario)
    state.update(
        scenario=scenario,
        confirm_count=0,
        transfer_post_count=0,
        transfer_execution_count=0,
        transfers={},
        transfer_request_ids={},
        conversation_transfer_confirm_count=0,
        conversation_transfer_execution_count=0,
        conversation_transfer=(
            _conversation_transfer("waiting_confirm")
            if scenario == "conversation_transfer_waiting"
            else _conversation_transfer("running")
            if scenario == "conversation_transfer_running"
            else _conversation_transfer("success")
            if scenario == "conversation_transfer_completed"
            else _conversation_transfer("failed")
            if scenario == "conversation_transfer_failed"
            else _conversation_transfer("cancelled")
            if scenario == "conversation_transfer_rejected"
            else None
        ),
        deployment_create_count=0,
        deployment_confirm_count=0,
        deployment_execution_count=0,
        deployment_rollback_count=0,
        deployment_run=_make_deployment_run(deployment_status) if deployment_status else None,
    )
    return dict(state)


@app.post("/__test__/complete")
async def complete_active_task() -> dict[str, Any]:
    """Advance the fake task after a refresh; no real work is performed."""
    state["scenario"] = "idle"
    payloads = [
        {
            "type": "agent",
            "timestamp": "09:00:03",
            "session_id": SESSION_ID,
            "turn_id": TASK_ID,
            "channel": "chat",
            "content": "演示任务已完成",
        },
        {
            "type": "turn_state",
            "timestamp": "09:00:04",
            "session_id": SESSION_ID,
            "turn_id": TASK_ID,
            "channel": "chat",
            "status": "completed",
            "label": "任务完成",
            "active": False,
        },
    ]
    delivered = 0
    for websocket in list(session_subscribers):
        try:
            for payload in payloads:
                await websocket.send_json(payload)
            delivered += 1
        except Exception:
            session_subscribers.discard(websocket)
    return {"ok": True, "delivered": delivered}


@app.get("/__test__/state")
async def test_state() -> dict[str, Any]:
    return dict(state)


@app.post("/__test__/deployment/advance")
async def advance_deployment(request: Request) -> dict[str, Any]:
    """Advance only the in-memory fixture; no executor or network is used."""
    run = state.get("deployment_run")
    if not run:
        return JSONResponse({"error": "no deployment run"}, status_code=404)
    payload = await request.json()
    status = str(payload.get("status") or "completed")
    if status not in {"staging_upload", "completed", "rollback_required", "rollback_running", "unknown"}:
        return JSONResponse({"error": "unsupported deployment status"}, status_code=400)
    updated = _make_deployment_run(status)
    updated["request_id"] = run.get("request_id") or updated["request_id"]
    state["deployment_run"] = updated
    state["scenario"] = f"deployment_{status}"
    return {"ok": True, "run": updated}


@app.post("/__test__/transfer/finish")
async def finish_transfer(request: Request) -> dict[str, Any]:
    """Finish the newest fake transfer without touching SSH or the filesystem."""
    payload = await request.json()
    outcome = str(payload.get("outcome") or "success")
    transfers = _transfers()
    if not transfers:
        return JSONResponse({"error": "no transfer"}, status_code=404)
    transfer = state["transfers"][transfers[0]["id"]]
    transfer["updated_at"] = "2026-07-16T09:00:04"
    transfer["completed_at"] = "2026-07-16T09:00:04"
    if outcome == "success":
        transfer.update(
            status="success",
            remote_size=transfer["size"],
            remote_sha256=transfer["sha256"],
            error="",
        )
    elif outcome == "failed":
        transfer.update(
            status="failed",
            remote_size=0,
            remote_sha256="",
            error=str(payload.get("error") or "Permission denied"),
        )
    else:
        return JSONResponse({"error": "unknown outcome"}, status_code=400)
    return {"ok": True, "transfer": _transfer_public(transfer)}


@app.post("/__test__/conversation-transfer/finish")
async def finish_conversation_transfer(request: Request) -> dict[str, Any]:
    """Finish the chat-driven fixture without creating an SSH executor."""
    payload = await request.json()
    outcome = str(payload.get("outcome") or "success")
    if outcome not in {"success", "failed"}:
        return JSONResponse({"error": "unknown outcome"}, status_code=400)
    transfer = state.get("conversation_transfer")
    if not transfer or transfer.get("status") != "running":
        return JSONResponse({"error": "no running conversational transfer"}, status_code=409)
    updated = _conversation_transfer(outcome)
    state["conversation_transfer"] = updated
    state["scenario"] = "conversation_transfer_completed" if outcome == "success" else "conversation_transfer_failed"
    artifact = {
        "id": updated["id"], "transfer_id": updated["id"],
        "file_id": FILE_ID, "file_name": FILE_NAME, "target": "fake-host",
        "remote_path": CONVERSATION_REMOTE_PATH, "status": outcome,
        "remote_size": updated["remote_size"], "remote_sha256": updated["remote_sha256"],
        "error": updated["error"],
    }
    delivered = 0
    for websocket in list(session_subscribers):
        try:
            await websocket.send_json({
                "type": "artifact_upload", "timestamp": "09:00:04",
                "session_id": SESSION_ID, "turn_id": TASK_ID, "channel": "chat",
                "content": updated["error"] or "文件传输完成", "artifact": artifact,
            })
            await websocket.send_json({
                "type": "turn_state", "timestamp": "09:00:05",
                "session_id": SESSION_ID, "turn_id": TASK_ID, "channel": "chat",
                "status": "completed" if outcome == "success" else "failed",
                "label": "文件上传完成" if outcome == "success" else "Permission denied",
                "active": False, "transfer_id": CONVERSATION_TRANSFER_ID,
            })
            delivered += 1
        except Exception:
            session_subscribers.discard(websocket)
    return {"ok": True, "delivered": delivered, "transfer": updated}


@app.get("/api/sessions")
async def sessions() -> dict[str, Any]:
    detail = _session_detail()
    return {"sessions": [{key: detail[key] for key in ("id", "type", "title", "created_at", "updated_at")}]}


@app.get("/api/servers")
async def servers() -> dict[str, list[dict[str, Any]]]:
    return {"servers": [_server()]}


@app.get("/api/services")
async def services() -> dict[str, list[Any]]:
    return {"services": [_service()]}


@app.get("/api/credentials")
async def credentials() -> dict[str, list[Any]]:
    return {"credentials": []}


@app.get("/api/sessions/{session_id}/files")
async def session_files(session_id: str) -> dict[str, list[Any]]:
    if session_id != SESSION_ID:
        return JSONResponse({"error": "session not found"}, status_code=404)
    return {"files": [_session_file()]}


@app.get("/api/sessions/{session_id}/file-transfers")
async def session_file_transfers(session_id: str) -> dict[str, list[Any]]:
    if session_id != SESSION_ID:
        return JSONResponse({"error": "session not found"}, status_code=404)
    return {"transfers": _transfers()}


@app.get("/api/file-transfers/{transfer_id}")
async def file_transfer(transfer_id: str) -> dict[str, Any]:
    transfer = (state.get("transfers") or {}).get(transfer_id)
    if not transfer:
        return JSONResponse({"error": "transfer not found"}, status_code=404)
    return {"transfer": _transfer_public(transfer)}


def _deployment_response(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run": dict(run),
        "steps": [dict(step) for step in run.get("steps") or []],
        "events": [dict(event) for event in run.get("events") or []],
    }


@app.get("/api/deployment-runs")
async def deployment_runs(session_id: str = "", limit: int = 20) -> dict[str, list[Any]]:
    del limit
    run = state.get("deployment_run")
    if not run or (session_id and session_id != SESSION_ID):
        return {"runs": []}
    return {"runs": [dict(run)]}


@app.post("/api/deployment-runs")
async def create_deployment_run(request: Request) -> dict[str, Any]:
    state["deployment_create_count"] += 1
    payload = await request.json()
    if payload.get("session_id") != SESSION_ID or payload.get("file_id") != FILE_ID:
        return JSONResponse({"error": "session artifact not found"}, status_code=404)
    if payload.get("service_id") != _service()["id"]:
        return JSONResponse({"error": "service profile not found"}, status_code=404)
    existing = state.get("deployment_run")
    if existing:
        return _deployment_response(existing)
    run = _make_deployment_run("waiting_plan_confirm")
    run["request_id"] = str(payload.get("request_id") or run["request_id"])
    state["deployment_run"] = run
    state["scenario"] = "deployment_waiting"
    return _deployment_response(run)


@app.get("/api/deployment-runs/{run_id}")
async def deployment_run(run_id: str) -> dict[str, Any]:
    run = state.get("deployment_run")
    if not run or run_id != DEPLOYMENT_RUN_ID:
        return JSONResponse({"error": "deployment run not found"}, status_code=404)
    return _deployment_response(run)


@app.post("/api/deployment-runs/{run_id}/confirm")
async def confirm_deployment_run(run_id: str, request: Request) -> dict[str, Any]:
    state["deployment_confirm_count"] += 1
    run = state.get("deployment_run")
    if not run or run_id != DEPLOYMENT_RUN_ID:
        return JSONResponse({"error": "deployment run not found"}, status_code=404)
    payload = await request.json()
    if payload.get("plan_hash") != DEPLOYMENT_PLAN_HASH:
        return JSONResponse({"error": "frozen plan hash mismatch"}, status_code=409)
    if run["status"] != "waiting_plan_confirm":
        return JSONResponse({"error": "plan is no longer awaiting confirmation"}, status_code=409)
    # Keep the request open briefly so the browser must disable the action
    # locally and suppress a second click before the server responds.
    await asyncio.sleep(0.35)
    updated = _make_deployment_run("staging_upload")
    updated["request_id"] = run.get("request_id") or updated["request_id"]
    state["deployment_run"] = updated
    state["deployment_execution_count"] += 1
    state["scenario"] = "deployment_running"
    return _deployment_response(updated)


@app.post("/api/deployment-runs/{run_id}/cancel")
async def cancel_deployment_run(run_id: str) -> dict[str, Any]:
    run = state.get("deployment_run")
    if not run or run_id != DEPLOYMENT_RUN_ID:
        return JSONResponse({"error": "deployment run not found"}, status_code=404)
    updated = _make_deployment_run("canceled")
    state["deployment_run"] = updated
    return _deployment_response(updated)


@app.post("/api/deployment-runs/{run_id}/rollback/confirm")
async def confirm_deployment_rollback(run_id: str) -> dict[str, Any]:
    state["deployment_rollback_count"] += 1
    run = state.get("deployment_run")
    if not run or run_id != DEPLOYMENT_RUN_ID:
        return JSONResponse({"error": "deployment run not found"}, status_code=404)
    if run["status"] != "rollback_required":
        return JSONResponse({"error": "rollback is not awaiting confirmation"}, status_code=409)
    await asyncio.sleep(0.35)
    updated = _make_deployment_run("rollback_running")
    updated["request_id"] = run.get("request_id") or updated["request_id"]
    state["deployment_run"] = updated
    state["scenario"] = "deployment_rollback_running"
    return _deployment_response(updated)


@app.post("/api/sessions/{session_id}/files/{file_id}/transfers")
async def create_file_transfer(session_id: str, file_id: str, request: Request) -> dict[str, Any]:
    state["transfer_post_count"] += 1
    if session_id != SESSION_ID or file_id != FILE_ID:
        return JSONResponse({"error": "session file not found"}, status_code=404)
    payload = await request.json()
    target = str(payload.get("target") or "").strip()
    remote_dir = str(payload.get("remote_dir") or "").strip()
    remote_name = str(payload.get("remote_name") or FILE_NAME).strip()
    request_id = str(payload.get("request_id") or "").strip()
    if target != _server()["alias"]:
        return JSONResponse({"error": "unknown target"}, status_code=400)
    if not remote_dir.startswith("/"):
        return JSONResponse({"error": "remote directory must be absolute"}, status_code=400)
    if not request_id:
        return JSONResponse({"error": "request_id is required"}, status_code=400)

    existing_id = state["transfer_request_ids"].get(request_id)
    if existing_id:
        transfer = state["transfers"][existing_id]
        return {
            "ok": True,
            "transfer": _transfer_public(transfer),
            "message": "传输请求已存在",
        }

    transfer_id = f"transfer-{len(state['transfers']) + 1}"
    remote_path = posixpath.join(remote_dir.rstrip("/") or "/", remote_name)
    transfer = {
        "id": transfer_id,
        "request_id": request_id,
        "session_id": SESSION_ID,
        "file_id": FILE_ID,
        "filename": FILE_NAME,
        "target": target,
        "remote_dir": remote_dir.rstrip("/") or "/",
        "remote_path": remote_path,
        "overwrite": bool(payload.get("overwrite", False)),
        "status": "running",
        "size": FILE_SIZE,
        "sha256": FILE_SHA256,
        "remote_size": 0,
        "remote_sha256": "",
        "error": "",
        "created_at": "2026-07-16T09:00:02",
        "updated_at": "2026-07-16T09:00:02",
        "completed_at": "",
    }
    state["transfers"][transfer_id] = transfer
    state["transfer_request_ids"][request_id] = transfer_id
    state["transfer_execution_count"] += 1
    return {
        "ok": True,
        "transfer": _transfer_public(transfer),
        "message": f"正在传到 {target}:{remote_path}",
    }


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str) -> dict[str, Any]:
    if session_id != SESSION_ID:
        return JSONResponse({"error": "session not found"}, status_code=404)
    return {"session": _session_detail()}


@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "subscribe" and message.get("session_id") == SESSION_ID:
                session_subscribers.add(websocket)
                detail = _session_detail()
                await websocket.send_json(
                    {
                        "type": "session_sync",
                        "session_id": SESSION_ID,
                        "channel": message.get("channel") or "chat",
                        "messages": detail["messages"],
                        "pending": detail["pending"],
                        "tasks": detail["tasks"],
                    }
                )
                continue
            if message.get("type") == "chat" and state.get("scenario") == "conversation_transfer_idle":
                transfer = _conversation_transfer("waiting_confirm")
                state["conversation_transfer"] = transfer
                state["scenario"] = "conversation_transfer_waiting"
                await websocket.send_json({
                    "type": "user_message", "timestamp": "09:00:00",
                    "session_id": SESSION_ID, "turn_id": TASK_ID, "channel": "chat",
                    "content": str(message.get("message") or ""),
                })
                await websocket.send_json({
                    "type": "file_transfer_preview", "timestamp": "09:00:01",
                    "session_id": SESSION_ID, **_conversation_transfer_preview(transfer),
                })
                await websocket.send_json({
                    "type": "turn_state", "timestamp": "09:00:02",
                    "session_id": SESSION_ID, "turn_id": TASK_ID, "channel": "chat",
                    "status": "waiting_confirm", "label": "等待确认文件上传", "active": True,
                    "transfer_id": CONVERSATION_TRANSFER_ID,
                })
                continue
            if message.get("type") == "file_transfer_confirm":
                state["conversation_transfer_confirm_count"] += 1
                transfer = state.get("conversation_transfer")
                if not transfer or message.get("transfer_id") != CONVERSATION_TRANSFER_ID:
                    await websocket.send_json({
                        "type": "file_transfer_confirm_ack", "session_id": SESSION_ID,
                        "transfer_id": str(message.get("transfer_id") or ""),
                        "request_id": str(message.get("request_id") or ""),
                        "accepted": False, "duplicate": False, "status": "not_found",
                        "content": "待确认文件传输不存在",
                    })
                    continue
                if state["conversation_transfer_confirm_count"] != 1:
                    continue
                await asyncio.sleep(0.35)
                confirmed = bool(message.get("confirmed"))
                status = "running" if confirmed else "cancelled"
                state["conversation_transfer"] = _conversation_transfer(status)
                state["scenario"] = "conversation_transfer_running" if confirmed else "conversation_transfer_rejected"
                if confirmed:
                    state["conversation_transfer_execution_count"] += 1
                await websocket.send_json({
                    "type": "turn_state", "timestamp": "09:00:03",
                    "session_id": SESSION_ID, "turn_id": TASK_ID, "channel": "chat",
                    "status": "executing" if confirmed else "canceled",
                    "label": "正在上传文件" if confirmed else "已取消文件上传",
                    "active": confirmed, "transfer_id": CONVERSATION_TRANSFER_ID,
                })
                await websocket.send_json({
                    "type": "file_transfer_confirm_ack", "timestamp": "09:00:03",
                    "session_id": SESSION_ID, "channel": "chat",
                    "transfer_id": CONVERSATION_TRANSFER_ID,
                    "request_id": str(message.get("request_id") or ""),
                    "confirmed": confirmed, "accepted": True, "duplicate": False,
                    "status": status, "content": "确认请求已受理" if confirmed else "文件传输已取消",
                    "transfer": state["conversation_transfer"],
                })
                continue
            if message.get("type") != "confirm":
                continue
            state["confirm_count"] += 1
            if state["confirm_count"] != 1:
                continue
            # Leave a deterministic window in which the client must display
            # its local submitting state and suppress a second click.
            await asyncio.sleep(0.35)
            state["scenario"] = "active"
            await websocket.send_json(
                {
                    "type": "turn_state",
                    "timestamp": "09:00:02",
                    "session_id": SESSION_ID,
                    "turn_id": TASK_ID,
                    "channel": "chat",
                    "status": "executing",
                    "label": "正在执行命令",
                    "active": True,
                }
            )
    except WebSocketDisconnect:
        session_subscribers.discard(websocket)
        return


@app.get("/assets/logo.svg")
async def logo() -> FileResponse:
    return FileResponse(STATIC_ROOT / "assets" / "logo.svg", media_type="image/svg+xml")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/next/#/chat")


app.mount("/next", StaticFiles(directory=BUILD_ROOT, html=True), name="workbench")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("SHELL_AGENT_E2E_HOST", "127.0.0.1"),
        port=int(os.environ.get("SHELL_AGENT_E2E_PORT", "4178")),
        log_level="warning",
    )
