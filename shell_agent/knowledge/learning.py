"""Background knowledge learning from completed Shell Agent tasks."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from shell_agent.core.context import compact_text
from shell_agent.knowledge.policy import normalize_extracted_knowledge
from shell_agent.knowledge.redaction import SecretRedactor
from shell_agent.storage.memories import upsert_memory
from shell_agent.storage.profile_candidates import create_profile_candidate
from shell_agent.storage.tasks import get_task_events


_LEARNING_SIGNALS = (
    "安装",
    "部署",
    "配置",
    "路径",
    "目录",
    "端口",
    "版本",
    "日志",
    "服务",
    "systemd",
    "docker",
    "tomcat",
    "nginx",
    "mysql",
    "java",
    "jdk",
    "install",
    "deploy",
    "version",
)


@dataclass
class LearningResult:
    memories: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def memory_count(self) -> int:
        return len(self.memories)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)


def should_learn_from_task(user_input: str, task_outputs: str) -> bool:
    text = f"{user_input} {task_outputs[:3000]}".lower()
    return any(signal in text for signal in _LEARNING_SIGNALS)


async def _task_evidence(db: aiosqlite.Connection, task_id: str) -> str:
    events = await get_task_events(db, task_id)
    chunks: list[str] = []
    for event in events:
        if event.get("type") != "execution_result":
            continue
        payload = event.get("payload") or {}
        status = str(event.get("status") or "")
        if status not in {"success", "partial"} and not payload.get("partial_success"):
            continue
        chunks.append(
            "\n".join(
                [
                    f"target: {payload.get('target') or ''}",
                    f"command: {payload.get('command') or ''}",
                    f"exit_code: {payload.get('exit_code')}",
                    "output:",
                    compact_text(str(payload.get("output") or event.get("content") or ""), limit=1800),
                ]
            )
        )
    return compact_text("\n\n".join(chunks), limit=6000)


def _service_id(value: str) -> str:
    normalized = re.sub(r"[^\w-]+", "-", value.strip().lower(), flags=re.UNICODE)
    return normalized.strip("-_") or "service"


def _service_snapshot(
    services: dict[str, Any],
    service_id: str,
    service_name: str,
    target: str,
) -> tuple[str, dict]:
    normalized_id = service_id.strip().lower()
    normalized_name = service_name.strip().lower()
    matches: list[tuple[str, dict]] = []
    for key, service in services.items():
        item = service.model_dump() if hasattr(service, "model_dump") else dict(service)
        if key.lower() == normalized_id or str(item.get("name") or "").lower() == normalized_name:
            matches.append((str(item.get("id") or key), item))

    if target:
        for existing_id, item in matches:
            if target in (item.get("servers") or []):
                return existing_id, item
    elif matches:
        return matches[0]

    base_id = _service_id(service_id or service_name)
    if target:
        target_id = _service_id(target)
        suffix = f"-{target_id}"
        instance_id = base_id if base_id.endswith(suffix) else f"{base_id}{suffix}"
        for key, service in services.items():
            item = service.model_dump() if hasattr(service, "model_dump") else dict(service)
            if str(item.get("id") or key).lower() == instance_id.lower():
                return str(item.get("id") or key), item
        return instance_id, {}
    return base_id, {}


def _merge_profile_changes(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, list):
            merged[key] = list(dict.fromkeys([*current, *value]))
        else:
            merged[key] = value
    return merged


async def learn_from_task(
    db: aiosqlite.Connection,
    llm,
    *,
    task_id: str,
    session_id: str,
    user_input: str,
    final_summary: str,
    services: dict[str, Any],
    servers: dict[str, Any],
    secret_values: list[str] | None = None,
) -> LearningResult:
    result = LearningResult()
    if not task_id or not hasattr(llm, "extract_knowledge"):
        return result
    redactor = SecretRedactor(secret_values)
    task_outputs = redactor.redact_text(await _task_evidence(db, task_id))
    safe_input = redactor.redact_text(user_input)
    safe_summary = redactor.redact_text(final_summary)
    if not task_outputs or not should_learn_from_task(safe_input, task_outputs):
        return result
    try:
        extracted = await llm.extract_knowledge(
            user_input=safe_input,
            task_outputs=task_outputs,
            final_summary=safe_summary,
            server_aliases=list(servers.keys()),
            service_profiles=[
                service.model_dump() if hasattr(service, "model_dump") else dict(service)
                for service in services.values()
            ],
        )
    except Exception as exc:
        result.errors.append(str(exc))
        return result
    if not isinstance(extracted, dict) or not isinstance(extracted.get("memories"), list):
        result.errors.append("知识提取结果格式无效")
        return result

    candidate_groups: dict[str, dict[str, Any]] = {}
    for raw in extracted["memories"][:12]:
        if not isinstance(raw, dict):
            continue
        item = normalize_extracted_knowledge(
            redactor.redact(raw),
            server_aliases=set(servers.keys()),
            redactor=redactor,
        )
        if not item:
            continue
        memory = await upsert_memory(
            db,
            subject=item["subject"],
            predicate=item["predicate"],
            value=item["value"],
            target=item["target"],
            memory_type=item["type"],
            status="inferred",
            confidence=item["confidence"],
            source_session_id=session_id,
            source_task_id=task_id,
            source="task_learning",
            expires_at=item["expires_at"],
            evidence_summary=item["evidence_summary"],
        )
        result.memories.append(memory)
        if not item["profile_changes"]:
            continue
        service_id, before = _service_snapshot(
            services,
            item["service_id"],
            item["service_name"],
            item["target"],
        )
        group = candidate_groups.setdefault(
            service_id,
            {
                "service_id": service_id,
                "service_name": item["service_name"],
                "proposed_changes": {},
                "before_snapshot": before,
                "targets": [],
                "summaries": [],
                "confidence": 1.0,
                "source_memory_ids": [],
            },
        )
        group["proposed_changes"] = _merge_profile_changes(
            group["proposed_changes"], item["profile_changes"]
        )
        if item["target"] and item["target"] not in group["targets"]:
            group["targets"].append(item["target"])
        if item["evidence_summary"] and item["evidence_summary"] not in group["summaries"]:
            group["summaries"].append(item["evidence_summary"])
        group["confidence"] = min(group["confidence"], item["confidence"])
        memory_id = str(memory.get("id") or "")
        if memory_id:
            group["source_memory_ids"].append(memory_id)

    for group in candidate_groups.values():
        candidate, created = await create_profile_candidate(
            db,
            service_id=group["service_id"],
            service_name=group["service_name"],
            proposed_changes=group["proposed_changes"],
            before_snapshot=group["before_snapshot"],
            evidence={
                "task_id": task_id,
                "session_id": session_id,
                "target": ", ".join(group["targets"]),
                "summary": "；".join(group["summaries"]),
            },
            confidence=group["confidence"],
            source_memory_ids=group["source_memory_ids"],
            source_task_id=task_id,
        )
        if created:
            result.candidates.append(candidate)
    return result
