"""Server inventory and service-profile REST endpoints."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import re
import time
from uuid import uuid4

import yaml
from fastapi import APIRouter
from loguru import logger

from shell_agent.core.models import PendingCommand
from shell_agent.executors.ssh import SSHExecutor
from shell_agent.utils.config import ServerEntry
from shell_agent.web.routes.credentials import mask_credential, read_credentials_file
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import ServerCreate, ServiceProfileUpsert


router = APIRouter()
SERVER_CONNECTION_TEST_TIMEOUT_SECONDS = 12
SERVER_CONNECTION_TEST_MARKER = "__opsane_ssh_ready__"


def inventory_path() -> Path:
    return Path("config/inventory.yaml")


def read_inventory_file() -> dict:
    path = inventory_path()
    if not path.exists():
        return {"servers": [], "services": []}
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    data.setdefault("servers", [])
    data.setdefault("services", [])
    return data


def write_inventory_file(data: dict) -> None:
    path = inventory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
        with temp_path.open(encoding="utf-8") as stream:
            validated = yaml.safe_load(stream) or {}
        if not isinstance(validated, dict):
            raise ValueError("服务清单写入校验失败")
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _services_referencing_server(data: dict, alias: str) -> list[str]:
    return [
        str(item.get("name") or item.get("id") or "未命名服务")
        for item in data.get("services", [])
        if alias in (item.get("servers") or [])
    ]


def _replace_server_references(data: dict, old_alias: str, new_alias: str) -> None:
    for service in data.get("services", []):
        servers = list(service.get("servers") or [])
        if old_alias not in servers:
            continue
        service["servers"] = list(
            dict.fromkeys(new_alias if alias == old_alias else alias for alias in servers)
        )
        try:
            service["revision"] = max(1, int(service.get("revision") or 1)) + 1
        except (TypeError, ValueError):
            service["revision"] = 2


@router.get("/api/servers")
async def list_servers() -> dict:
    rt = get_runtime()
    credentials = {
        item.get("id", ""): mask_credential(item)
        for item in read_credentials_file().get("credentials", [])
    }
    servers = []
    for server in rt.servers.values():
        credential = credentials.get(server.ssh_credential, {})
        servers.append({
            "alias": server.alias,
            "host": server.host,
            "port": server.port,
            "env": server.env,
            "role": server.role,
            "ssh_credential": server.ssh_credential,
            "ssh_username": credential.get("username", ""),
            "ssh_auth_type": credential.get("type", ""),
            "ssh_password_set": credential.get("password_set", False),
            "ssh_private_key_set": credential.get("private_key_set", False),
            "ssh_passphrase_set": credential.get("passphrase_set", False),
            "tags": server.tags,
        })
    return {"servers": servers}


async def _probe_server_connection(rt, server: ServerCreate) -> dict:
    alias = server.alias.strip()
    host = server.host.strip()
    credential_id = server.ssh_credential.strip()
    if not alias:
        return {"ok": False, "error": "服务器别名必填"}
    if not host:
        return {"ok": False, "error": "服务器主机地址必填"}
    if not 1 <= server.port <= 65535:
        return {"ok": False, "error": "SSH 端口必须在 1 到 65535 之间"}
    credential = rt.credentials.get(credential_id)
    if credential is None:
        return {"ok": False, "error": f"SSH 凭证 {credential_id or '-'} 不存在"}

    entry = ServerEntry(
        **{
            **server.model_dump(),
            "alias": alias,
            "host": host,
            "ssh_credential": credential_id,
        }
    )
    timeout = min(
        max(1, int(rt.config.ssh.default_timeout)),
        SERVER_CONNECTION_TEST_TIMEOUT_SECONDS,
    )
    executor = SSHExecutor(
        servers={alias: entry},
        credentials={credential_id: credential},
        max_per_host=1,
        idle_timeout=rt.config.ssh.idle_timeout,
        total_max=1,
        default_timeout=timeout,
        trust_unknown_hosts=rt.config.ssh.trust_unknown_hosts,
    )
    command = PendingCommand(
        raw=f"ssh {alias} 'printf {SERVER_CONNECTION_TEST_MARKER}'",
        target=alias,
        target_env=entry.env,
        executor="ssh",
        actual_command=f"printf {SERVER_CONNECTION_TEST_MARKER}",
        source="system",
        intent="测试 SSH 连通性",
    )
    started_at = time.monotonic()
    try:
        result = await asyncio.wait_for(
            executor.execute(command, timeout=timeout),
            timeout=timeout + 1,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"SSH 连接超时（{timeout} 秒）"}
    except Exception as error:
        detail = re.sub(r"\s+", " ", str(error)).strip()[:240]
        logger.warning("SSH 连通性测试失败: alias={} error={}", alias, detail)
        return {
            "ok": False,
            "error": f"SSH 连接失败：{detail or error.__class__.__name__}",
        }
    finally:
        await executor.close_all()

    latency_ms = max(0, int((time.monotonic() - started_at) * 1000))
    if result.timed_out:
        return {"ok": False, "error": f"SSH 探测命令超时（{timeout} 秒）"}
    if result.exit_code != 0:
        detail = re.sub(r"\s+", " ", result.stderr or "").strip()[:240]
        return {
            "ok": False,
            "error": f"SSH 探测失败：{detail or f'exit {result.exit_code}'}",
        }
    if SERVER_CONNECTION_TEST_MARKER not in result.stdout:
        return {"ok": False, "error": "SSH 已连接，但探测结果不完整"}
    return {
        "ok": True,
        "message": f"SSH 连接成功（{latency_ms} ms）",
        "latency_ms": latency_ms,
    }


@router.post("/api/servers/test-connection")
async def test_server_connection(server: ServerCreate) -> dict:
    return await _probe_server_connection(get_runtime(), server)


@router.post("/api/servers")
async def add_server(server: ServerCreate) -> dict:
    rt = get_runtime()
    if server.alias in rt.servers:
        return {"error": f"别名 {server.alias} 已存在"}
    data = read_inventory_file()
    data["servers"].append(server.model_dump())
    write_inventory_file(data)
    await rt.reload()
    return {"ok": True}


@router.put("/api/servers/{alias}")
async def update_server(alias: str, server: ServerCreate) -> dict:
    rt = get_runtime()
    if alias not in rt.servers:
        return {"error": f"别名 {alias} 不存在"}
    if server.alias != alias and server.alias in rt.servers:
        return {"error": f"别名 {server.alias} 已存在"}
    data = read_inventory_file()
    updated = False
    for index, item in enumerate(data.get("servers", [])):
        if item.get("alias") == alias:
            data["servers"][index] = server.model_dump()
            if server.alias != alias:
                _replace_server_references(data, alias, server.alias)
            updated = True
            break
    if not updated:
        return {"error": f"别名 {alias} 不存在"}
    write_inventory_file(data)
    await rt.reload()
    return {"ok": True}


@router.delete("/api/servers/{alias}")
async def delete_server(alias: str) -> dict:
    rt = get_runtime()
    if alias not in rt.servers:
        return {"error": f"别名 {alias} 不存在"}
    data = read_inventory_file()
    references = _services_referencing_server(data, alias)
    if references:
        return {
            "ok": False,
            "error": f"服务器 {alias} 仍被服务画像引用: {', '.join(references)}",
        }
    data["servers"] = [item for item in data.get("servers", []) if item.get("alias") != alias]
    write_inventory_file(data)
    await rt.reload()
    return {"ok": True}


def _service_id_from_name(name: str) -> str:
    service_id = re.sub(r"[^\w-]+", "-", name.strip().lower(), flags=re.UNICODE)
    service_id = service_id.strip("-_")
    return service_id or f"service-{uuid4().hex[:8]}"


def _normalize_service(service: ServiceProfileUpsert) -> dict:
    data = service.model_dump()
    data["id"] = (data.get("id") or _service_id_from_name(data["name"])).strip()
    data["name"] = data["name"].strip()
    data["env"] = (data.get("env") or "dev").strip() or "dev"
    data["owners"] = [item.strip() for item in data.get("owners", []) if item.strip()]
    data["servers"] = [item.strip() for item in data.get("servers", []) if item.strip()]
    ports = []
    for port in data.get("ports", []):
        value = int(port)
        if value > 0 and value not in ports:
            ports.append(value)
    data["ports"] = ports
    data["config_paths"] = [
        item.strip() for item in data.get("config_paths", []) if item.strip()
    ]
    data["tags"] = [item.strip() for item in data.get("tags", []) if item.strip()]
    for key in [
        "deploy_dir", "artifact_path", "backup_dir", "artifact_type",
        "log_dir", "health_url", "start_cmd", "stop_cmd",
        "restart_cmd", "status_cmd", "runtime", "version", "last_verified_at",
        "source_task_id", "notes",
    ]:
        data[key] = (data.get(key) or "").strip()
    status = (data.get("verification_status") or "unknown").strip().lower()
    data["verification_status"] = (
        status if status in {"verified", "stale", "conflicted", "unknown"} else "unknown"
    )
    data["revision"] = max(1, int(data.get("revision") or 1))
    return data


def _validate_service_payload(data: dict, server_aliases: set[str]) -> str:
    if not data["name"]:
        return "服务名称必填"
    if "/" in data["id"]:
        return "服务标识不能包含 /"
    unknown_servers = [alias for alias in data["servers"] if alias not in server_aliases]
    if unknown_servers:
        return f"服务绑定了不存在的服务器: {', '.join(unknown_servers)}"
    return ""


class ServiceRevisionConflict(ValueError):
    pass


async def apply_service_profile_changes(
    *,
    service_id: str,
    service_name: str,
    changes: dict,
    expected_revision: int | None = None,
    source_task_id: str = "",
) -> dict:
    """Apply a reviewed profile patch and reload the runtime."""
    rt = get_runtime()
    data = read_inventory_file()
    current_index = next(
        (
            index
            for index, item in enumerate(data.get("services", []))
            if item.get("id") == service_id
        ),
        None,
    )
    current = data["services"][current_index] if current_index is not None else {}
    current_revision = max(1, int(current.get("revision") or 1)) if current else 0
    if expected_revision is not None and expected_revision != current_revision:
        raise ServiceRevisionConflict(
            f"服务画像已更新，期望 revision={expected_revision}，当前 revision={current_revision}"
        )

    merged = {**current, **changes}
    merged["id"] = service_id or str(merged.get("id") or "")
    merged["name"] = service_name or str(merged.get("name") or "")
    merged["revision"] = current_revision + 1 if current else 1
    if source_task_id:
        merged["source_task_id"] = source_task_id
        merged["last_verified_at"] = datetime.now().isoformat(timespec="seconds")
        merged["verification_status"] = "verified"
    payload = _normalize_service(ServiceProfileUpsert(**merged))
    error = _validate_service_payload(payload, set(rt.servers.keys()))
    if error:
        raise ValueError(error)
    duplicate = next(
        (
            item
            for index, item in enumerate(data.get("services", []))
            if item.get("id") == payload["id"] and index != current_index
        ),
        None,
    )
    if duplicate:
        raise ValueError(f"服务 {payload['id']} 已存在")
    if current_index is None:
        data["services"].append(payload)
    else:
        data["services"][current_index] = payload
    write_inventory_file(data)
    await rt.reload()
    return payload


@router.get("/api/services")
async def list_services() -> dict:
    rt = get_runtime()
    services = [service.model_dump() for service in getattr(rt, "services", {}).values()]
    return {"services": services}


@router.post("/api/services")
async def add_service(service: ServiceProfileUpsert) -> dict:
    rt = get_runtime()
    data = read_inventory_file()
    payload = _normalize_service(service)
    error = _validate_service_payload(payload, set(rt.servers.keys()))
    if error:
        return {"ok": False, "error": error}
    if any(item.get("id") == payload["id"] for item in data.get("services", [])):
        return {"ok": False, "error": f"服务 {payload['id']} 已存在"}
    payload["revision"] = 1
    data["services"].append(payload)
    write_inventory_file(data)
    await rt.reload()
    return {"ok": True, "service": payload}


@router.put("/api/services/{service_id}")
async def update_service(service_id: str, service: ServiceProfileUpsert) -> dict:
    rt = get_runtime()
    data = read_inventory_file()
    payload = _normalize_service(service)
    error = _validate_service_payload(payload, set(rt.servers.keys()))
    if error:
        return {"ok": False, "error": error}
    if payload["id"] != service_id and any(
        item.get("id") == payload["id"] for item in data.get("services", [])
    ):
        return {"ok": False, "error": f"服务 {payload['id']} 已存在"}
    updated = False
    for index, item in enumerate(data.get("services", [])):
        if item.get("id") == service_id:
            payload["revision"] = max(1, int(item.get("revision") or 1)) + 1
            data["services"][index] = payload
            updated = True
            break
    if not updated:
        return {"ok": False, "error": f"服务 {service_id} 不存在"}
    write_inventory_file(data)
    await rt.reload()
    return {"ok": True, "service": payload}


@router.delete("/api/services/{service_id}")
async def delete_service(service_id: str) -> dict:
    data = read_inventory_file()
    before_count = len(data.get("services", []))
    data["services"] = [item for item in data.get("services", []) if item.get("id") != service_id]
    if len(data["services"]) == before_count:
        return {"ok": False, "error": f"服务 {service_id} 不存在"}
    write_inventory_file(data)
    await get_runtime().reload()
    return {"ok": True}
