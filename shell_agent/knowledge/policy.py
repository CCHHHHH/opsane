"""Validation and normalization for LLM-extracted knowledge."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from shell_agent.knowledge.redaction import REDACTED, SecretRedactor, is_sensitive_predicate
from shell_agent.storage.memories import MEMORY_TYPES, normalize_key


PROFILE_FIELDS = {
    "servers",
    "deploy_dir",
    "artifact_path",
    "backup_dir",
    "artifact_type",
    "startup_timeout_seconds",
    "log_dir",
    "health_url",
    "ports",
    "start_cmd",
    "stop_cmd",
    "restart_cmd",
    "status_cmd",
    "config_paths",
    "runtime",
    "version",
    "tags",
    "notes",
}
_LIST_FIELDS = {"servers", "ports", "config_paths", "tags"}
_PATH_FIELDS = {"deploy_dir", "artifact_path", "backup_dir", "log_dir"}
_TRANSIENT_PREDICATES = {
    "pid",
    "cpu",
    "cpu_usage",
    "memory_usage",
    "disk_usage",
    "task_status",
    "running_status",
}


def _expires_at(memory_type: str, predicate: str) -> str:
    now = datetime.now()
    if memory_type == "preference" or memory_type == "procedure":
        return ""
    if memory_type == "observation" or predicate in _TRANSIENT_PREDICATES:
        return (now + timedelta(hours=1)).isoformat(timespec="seconds")
    if any(word in predicate for word in ("version", "port", "版本", "端口")):
        return (now + timedelta(days=30)).isoformat(timespec="seconds")
    if any(word in predicate for word in ("path", "dir", "directory", "目录", "路径")):
        return (now + timedelta(days=90)).isoformat(timespec="seconds")
    return ""


def _unique_strings(values: list[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in output:
            output.append(item)
    return output


def _normalize_profile_changes(
    raw: dict,
    *,
    server_aliases: set[str],
    redactor: SecretRedactor,
) -> dict:
    output: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in PROFILE_FIELDS or value in (None, "", []):
            continue
        value = redactor.redact(value)
        if REDACTED in str(value):
            continue
        if key == "servers":
            servers = _unique_strings(value if isinstance(value, list) else [value])
            servers = [alias for alias in servers if alias in server_aliases]
            if servers:
                output[key] = servers
        elif key == "ports":
            ports: list[int] = []
            for item in value if isinstance(value, list) else [value]:
                try:
                    port = int(item)
                except (TypeError, ValueError):
                    continue
                if 0 < port <= 65535 and port not in ports:
                    ports.append(port)
            if ports:
                output[key] = ports
        elif key in {"config_paths", "tags"}:
            items = _unique_strings(value if isinstance(value, list) else [value])
            if items:
                output[key] = items
        elif key in _PATH_FIELDS:
            path = str(value).strip()
            if path.startswith("/") and str(PurePosixPath(path)) == path.rstrip("/"):
                output[key] = path
        else:
            output[key] = str(value).strip()
    return output


def normalize_extracted_knowledge(
    raw: dict,
    *,
    server_aliases: set[str],
    redactor: SecretRedactor,
) -> dict | None:
    memory_type = normalize_key(str(raw.get("type") or "fact"))
    if memory_type not in MEMORY_TYPES:
        return None
    subject = normalize_key(str(raw.get("subject") or ""))
    predicate = normalize_key(str(raw.get("predicate") or "note")) or "note"
    value = redactor.redact_text(str(raw.get("value") or "").strip())
    target = str(raw.get("target") or "").strip()
    if not subject or not value or REDACTED in value or is_sensitive_predicate(predicate):
        return None
    if target and target not in server_aliases:
        return None
    try:
        confidence = max(0.0, min(float(raw.get("confidence", 0.7)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.7
    if confidence < 0.45:
        return None

    evidence_summary = redactor.redact_text(str(raw.get("evidence_summary") or "").strip())
    profile_changes = raw.get("profile_changes")
    if not isinstance(profile_changes, dict):
        profile_changes = {}
    profile_changes = _normalize_profile_changes(
        profile_changes,
        server_aliases=server_aliases,
        redactor=redactor,
    )
    service_name = str(raw.get("service_name") or subject).strip()
    service_id = str(raw.get("service_id") or "").strip()
    return {
        "type": memory_type,
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "target": target,
        "confidence": confidence,
        "evidence_summary": evidence_summary,
        "expires_at": _expires_at(memory_type, predicate),
        "service_id": service_id,
        "service_name": service_name,
        "profile_changes": profile_changes,
    }
