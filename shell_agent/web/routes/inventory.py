"""Server inventory and service-profile REST endpoints."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from uuid import uuid4

import yaml
from fastapi import APIRouter

from shell_agent.web.routes.credentials import mask_credential, read_credentials_file
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import ServerCreate, ServiceProfileUpsert


router = APIRouter()


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
