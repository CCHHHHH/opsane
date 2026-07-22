"""SSH credential REST endpoints."""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter

from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import CredentialUpsert


router = APIRouter()


def credentials_path() -> Path:
    return Path("config/credentials.yaml")


def read_credentials_file() -> dict:
    path = credentials_path()
    if not path.exists():
        return {"credentials": []}
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {"credentials": []}


def write_credentials_file(data: dict) -> None:
    with credentials_path().open("w", encoding="utf-8") as stream:
        yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)


def mask_credential(item: dict) -> dict:
    return {
        "id": item.get("id", ""),
        "type": item.get("type", "password"),
        "username": item.get("username", ""),
        "password_set": bool(item.get("password")),
        "private_key_set": bool(item.get("private_key")),
        "passphrase_set": bool(item.get("passphrase")),
    }


def _servers_referencing_credential(credential_id: str) -> list[str]:
    path = Path("config/inventory.yaml")
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    return [
        str(item.get("alias") or "未命名服务器")
        for item in data.get("servers", [])
        if item.get("ssh_credential") == credential_id
    ]


@router.get("/api/credentials")
async def list_credentials() -> dict:
    data = read_credentials_file()
    return {"credentials": [mask_credential(item) for item in data.get("credentials", [])]}


@router.post("/api/credentials")
async def upsert_credential(credential: CredentialUpsert) -> dict:
    rt = get_runtime()
    if credential.type not in ("password", "key"):
        return {"error": "凭证类型必须是 password 或 key"}
    if not credential.id.strip():
        return {"error": "凭证 ID 必填"}
    if not credential.username.strip():
        return {"error": "用户名必填"}

    data = read_credentials_file()
    items = data.setdefault("credentials", [])
    existing = next((item for item in items if item.get("id") == credential.id), None)
    if existing is None:
        existing = {"id": credential.id}
        items.append(existing)

    existing["id"] = credential.id.strip()
    existing["type"] = credential.type
    existing["username"] = credential.username.strip()
    if credential.type == "password":
        if credential.password:
            existing["password"] = credential.password
        existing.pop("private_key", None)
        existing.pop("passphrase", None)
    else:
        if credential.private_key:
            existing["private_key"] = credential.private_key
        if credential.passphrase:
            existing["passphrase"] = credential.passphrase
        existing.pop("password", None)

    write_credentials_file(data)
    await rt.reload()
    return {"ok": True}


@router.delete("/api/credentials/{credential_id}")
async def delete_credential(credential_id: str) -> dict:
    rt = get_runtime()
    references = _servers_referencing_credential(credential_id)
    if references:
        return {
            "ok": False,
            "error": f"凭证 {credential_id} 仍被服务器引用: {', '.join(references)}",
        }
    data = read_credentials_file()
    before = len(data.get("credentials", []))
    data["credentials"] = [
        item for item in data.get("credentials", []) if item.get("id") != credential_id
    ]
    if len(data["credentials"]) == before:
        return {"error": f"凭证 {credential_id} 不存在"}
    write_credentials_file(data)
    await rt.reload()
    return {"ok": True}
