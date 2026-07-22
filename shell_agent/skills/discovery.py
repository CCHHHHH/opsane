"""Discover deterministic Skill candidates from successful historical tasks."""
from __future__ import annotations

import hashlib
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from shell_agent.executors.ssh import parse_ssh_command
from shell_agent.knowledge.redaction import REDACTED, SecretRedactor
from shell_agent.safety.classifier import RiskLevel, classify_command
from shell_agent.skills.loader import parse_template_skill_data
from shell_agent.storage.skill_candidates import create_skill_candidate
from shell_agent.storage.tasks import get_task_events


_RISK_RANK = {
    RiskLevel.SAFE: 0,
    RiskLevel.CAUTION: 1,
    RiskLevel.DANGEROUS: 2,
    RiskLevel.CRITICAL: 3,
}
_VARIABLE_TOKEN_RE = re.compile(
    r"(?P<path>(?<![A-Za-z0-9_:/.])/[A-Za-z0-9._@:%+=,~/-]+)"
    r"|(?P<integer>(?<![A-Za-z0-9_./:-])\d+(?![A-Za-z0-9_./:-]))"
)
_TOKEN_MARKER_RE = re.compile(r"\x1f(path|integer):(\d+)\x1f")


@dataclass(frozen=True)
class HistoricalFlow:
    task_id: str
    session_id: str
    title: str
    targets: list[str]
    commands: list[str]
    template_commands: list[str]
    fingerprint: str
    risk_level: RiskLevel


@dataclass(frozen=True)
class CompiledFlowTemplate:
    template_commands: list[str]
    params: list[dict[str, Any]]
    fingerprint: str


def _normalize_command(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _template_command(raw: str, target_hint: str = "") -> tuple[str, str, str]:
    parsed = parse_ssh_command(raw)
    if parsed:
        target, actual = parsed
        normalized = _normalize_command(actual)
        return target, normalized, f"ssh {{{{target}}}} {shlex.quote(normalized)}"
    normalized = _normalize_command(raw)
    if target_hint:
        return target_hint, normalized, f"ssh {{{{target}}}} {shlex.quote(normalized)}"
    return "", normalized, normalized


def _flow_fingerprint(commands: list[str]) -> str:
    canonical = "\n".join(commands)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


async def _successful_flows(
    db: aiosqlite.Connection,
    *,
    days: int,
    redactor: SecretRedactor,
) -> list[HistoricalFlow]:
    cutoff = (datetime.now() - timedelta(days=max(1, min(days, 365)))).isoformat(timespec="seconds")
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT * FROM agent_tasks
        WHERE channel = 'chat'
          AND status IN ('completed', 'success')
          AND updated_at >= ?
        ORDER BY updated_at DESC
        LIMIT 1000
        """,
        (cutoff,),
    )
    flows: list[HistoricalFlow] = []
    for row in await cursor.fetchall():
        task = dict(row)
        snapshot = task.get("workflow_snapshot")
        if isinstance(snapshot, str) and '"source": "skill"' in snapshot:
            continue
        if isinstance(snapshot, dict) and snapshot.get("source") == "skill":
            continue
        events = await get_task_events(db, str(task["id"]))
        result_events = [event for event in events if event.get("type") == "execution_result"]
        if not result_events or any(event.get("status") != "success" for event in result_events):
            continue
        targets: list[str] = []
        commands: list[str] = []
        templates: list[str] = []
        highest_risk = RiskLevel.SAFE
        invalid = False
        for event in result_events:
            payload = event.get("payload") or {}
            raw = redactor.redact_text(str(payload.get("command") or ""))
            target_hint = redactor.redact_text(str(payload.get("target") or "")).strip()
            if not raw or REDACTED in raw:
                invalid = True
                break
            target, actual, template = _template_command(raw, target_hint)
            risk = classify_command(actual)
            if risk.level == RiskLevel.CRITICAL:
                invalid = True
                break
            if _RISK_RANK[risk.level] > _RISK_RANK[highest_risk]:
                highest_risk = risk.level
            if target and target not in targets:
                targets.append(target)
            commands.append(actual)
            templates.append(template)
        if invalid or not commands or len(commands) > 12:
            continue
        fingerprint = _flow_fingerprint(templates)
        flows.append(
            HistoricalFlow(
                task_id=str(task["id"]),
                session_id=str(task.get("session_id") or ""),
                title=redactor.redact_text(str(task.get("title") or "历史运维流程")),
                targets=targets,
                commands=commands,
                template_commands=templates,
                fingerprint=fingerprint,
                risk_level=highest_risk,
            )
        )
    return flows


def _tokenize_variable_candidates(command: str) -> tuple[str, list[tuple[str, str]]]:
    tokens: list[tuple[str, str]] = []
    parts: list[str] = []
    cursor = 0
    for match in _VARIABLE_TOKEN_RE.finditer(command):
        kind = "path" if match.group("path") is not None else "integer"
        value = match.group(0)
        parts.append(command[cursor : match.start()])
        parts.append(f"\x1f{kind}:{len(tokens)}\x1f")
        tokens.append((kind, value))
        cursor = match.end()
    parts.append(command[cursor:])
    return "".join(parts), tokens


def _compile_semantic_group(group: list[HistoricalFlow]) -> CompiledFlowTemplate | None:
    """Compile only path/integer differences; reject all structural ambiguity."""
    if len(group) < 2 or len({len(flow.commands) for flow in group}) != 1:
        return None
    step_count = len(group[0].commands)
    tokenized_steps: list[list[tuple[str, list[tuple[str, str]]]]] = []
    varying_vectors: dict[tuple[str, tuple[str, ...]], str] = {}
    kind_vectors: dict[str, set[tuple[str, ...]]] = defaultdict(set)

    for step_index in range(step_count):
        variants = [_tokenize_variable_candidates(flow.commands[step_index]) for flow in group]
        skeletons = {variant[0] for variant in variants}
        token_kinds = {tuple(kind for kind, _ in variant[1]) for variant in variants}
        remote_flags = {
            flow.template_commands[step_index].startswith("ssh {{target}} ")
            for flow in group
        }
        if len(skeletons) != 1 or len(token_kinds) != 1 or len(remote_flags) != 1:
            return None
        tokenized_steps.append(variants)
        token_count = len(variants[0][1])
        for token_index in range(token_count):
            kind = variants[0][1][token_index][0]
            values = tuple(variant[1][token_index][1] for variant in variants)
            if len(set(values)) > 1:
                kind_vectors[kind].add(values)

    # The first safe compiler supports at most one independent path and one
    # independent integer parameter. More variables require human design.
    if any(len(vectors) > 1 for vectors in kind_vectors.values()):
        return None
    if any(kind not in {"path", "integer"} for kind in kind_vectors):
        return None
    for kind, vectors in kind_vectors.items():
        for values in vectors:
            varying_vectors[(kind, values)] = "path" if kind == "path" else "number"

    params: list[dict[str, Any]] = []
    if kind_vectors.get("path"):
        params.append(
            {
                "name": "path",
                "type": "shell_path",
                "required": True,
                "description": "历史任务中变化的安全绝对路径",
            }
        )
    if kind_vectors.get("integer"):
        values = next(iter(kind_vectors["integer"]))
        numbers = [int(value) for value in values]
        params.append(
            {
                "name": "number",
                "type": "integer",
                "required": True,
                "description": "历史任务中变化的整数参数",
                "extract": r"(?P<number>\d{1,9})",
                "minimum": min(numbers),
                "maximum": max(numbers),
            }
        )

    compiled_commands: list[str] = []
    for step_index, variants in enumerate(tokenized_steps):
        skeleton, representative_tokens = variants[0]
        replacements: dict[str, str] = {}
        for token_index, (kind, value) in enumerate(representative_tokens):
            values = tuple(variant[1][token_index][1] for variant in variants)
            placeholder = varying_vectors.get((kind, values))
            replacements[f"\x1f{kind}:{token_index}\x1f"] = (
                "{{" + placeholder + "}}" if placeholder else value
            )
        actual_template = _TOKEN_MARKER_RE.sub(
            lambda match: replacements[match.group(0)],
            skeleton,
        )
        is_remote = group[0].template_commands[step_index].startswith("ssh {{target}} ")
        compiled_commands.append(
            f"ssh {{{{target}}}} {shlex.quote(actual_template)}"
            if is_remote
            else actual_template
        )
    if not params or compiled_commands == group[0].template_commands:
        return None
    return CompiledFlowTemplate(
        template_commands=compiled_commands,
        params=params,
        fingerprint=_flow_fingerprint(compiled_commands),
    )


def _clean_model_text(value: Any, redactor: SecretRedactor, limit: int) -> str:
    text = redactor.redact_text(str(value or ""))
    if REDACTED in text:
        return ""
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _draft_yaml(
    group: list[HistoricalFlow],
    *,
    compiled: CompiledFlowTemplate | None = None,
    label: str = "",
    model_description: str = "",
) -> tuple[str, str, str]:
    representative = group[0]
    template_commands = compiled.template_commands if compiled else representative.template_commands
    fingerprint = compiled.fingerprint if compiled else representative.fingerprint
    name = f"learned_{fingerprint[:10]}"
    description = model_description or f"从 {len(group)} 次成功历史任务中提炼：{representative.title[:80]}"
    triggers: list[str] = []
    # Keep a real historical request first so the non-executing preview can
    # exercise parameters before using the model's shorter semantic label.
    for text in [*(flow.title for flow in group), label]:
        title = text.strip()
        if title and title not in triggers:
            triggers.append(title[:80])
        if len(triggers) >= 5:
            break
    uses_target = any("{{target}}" in command for command in template_commands)
    data: dict[str, Any] = {
        "name": name,
        "version": "1",
        "description": description,
        "category": "learned",
        "enabled": False,
        "triggers": triggers or [representative.title[:80]],
        "params": [],
        "steps": [],
        "safety": {"default_confirm_mode": "interactive"},
    }
    if uses_target:
        data["params"].append(
            {
                "name": "target",
                "type": "server_alias",
                "required": True,
                "description": "目标服务器别名",
            }
        )
    if compiled:
        data["params"].extend(compiled.params)
    for index, command in enumerate(template_commands, start=1):
        risk = classify_command(representative.commands[index - 1])
        data["steps"].append(
            {
                "name": f"历史流程步骤 {index}",
                "command": command,
                "intent": f"执行历史流程步骤 {index}",
                "explanation": "根据成功历史任务生成，发布前需人工审核命令与成功判据。",
                "confirm": risk.level != RiskLevel.SAFE,
                "timeout_seconds": 60,
                "on_failure": "abort",
            }
        )
    raw = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    parse_template_skill_data(data, Path(f"{name}.yaml"))
    return name, description, raw


async def _persist_group_candidate(
    db: aiosqlite.Connection,
    *,
    group: list[HistoricalFlow],
    days: int,
    mode: str,
    redactor: SecretRedactor,
    compiled: CompiledFlowTemplate | None = None,
    label: str = "",
    model_description: str = "",
    rationale: str = "",
) -> tuple[dict, bool]:
    name, description, draft = _draft_yaml(
        group,
        compiled=compiled,
        label=label,
        model_description=model_description,
    )
    fingerprint = compiled.fingerprint if compiled else group[0].fingerprint
    risk = max(group, key=lambda item: _RISK_RANK[item.risk_level]).risk_level
    titles = list(dict.fromkeys(flow.title for flow in group))[:10]
    targets = list(dict.fromkeys(target for flow in group for target in flow.targets))
    candidate, was_created = await create_skill_candidate(
        db,
        name=name,
        description=description,
        fingerprint=fingerprint,
        draft_yaml=draft,
        evidence={
            "days": days,
            "grouping_mode": mode,
            "occurrences": len(group),
            "successful_occurrences": len(group),
            "titles": titles,
            "targets": targets,
            "commands": group[0].commands,
            "rationale": redactor.redact_text(rationale)[:500],
            "parameterized_fields": [item["name"] for item in (compiled.params if compiled else [])],
            "note": "候选仅来自全部步骤成功的任务；发布前仍需人工验证参数、命令与成功判据。",
        },
        confidence=(
            min(0.90, 0.50 + len(group) * 0.07)
            if mode == "semantic"
            else min(0.95, 0.55 + len(group) * 0.08)
        ),
        risk_level=risk.value,
        source_task_ids=[flow.task_id for flow in group],
    )
    return candidate, was_created


def _semantic_flow_records(flows: list[HistoricalFlow]) -> list[dict[str, Any]]:
    return [
        {
            "task_id": flow.task_id,
            "title": flow.title[:160],
            "step_count": len(flow.commands),
            "commands": [command[:500] for command in flow.commands],
        }
        for flow in flows
    ]


async def discover_skill_candidates(
    db: aiosqlite.Connection,
    *,
    days: int = 30,
    min_occurrences: int = 3,
    secret_values: list[str] | None = None,
    semantic: bool = False,
    llm: Any = None,
) -> dict:
    """Create disabled candidates from repeated, fully successful command flows.

    Exact grouping is always deterministic. Optional semantic grouping lets an
    LLM select task-id groups, but a local compiler accepts only structural
    equality with bounded path/integer differences.
    """
    min_occurrences = max(2, min(int(min_occurrences), 20))
    redactor = SecretRedactor(secret_values)
    flows = await _successful_flows(db, days=days, redactor=redactor)
    grouped: dict[str, list[HistoricalFlow]] = defaultdict(list)
    for flow in flows:
        grouped[flow.fingerprint].append(flow)

    created: list[dict] = []
    existing: list[dict] = []
    exact_groups = [group for group in grouped.values() if len(group) >= min_occurrences]
    exact_task_ids = {flow.task_id for group in exact_groups for flow in group}
    for group in exact_groups:
        candidate, was_created = await _persist_group_candidate(
            db,
            group=group,
            days=days,
            mode="exact",
            redactor=redactor,
        )
        (created if was_created else existing).append(candidate)

    semantic_result: dict[str, Any] = {
        "requested": bool(semantic),
        "status": "disabled" if not semantic else "unavailable",
        "submitted_flows": 0,
        "returned_groups": 0,
        "compiled_groups": 0,
        "rejected_groups": 0,
    }
    semantic_groups = 0
    semantic_pool = [flow for flow in flows if flow.task_id not in exact_task_ids][:80]
    cluster_method = getattr(llm, "cluster_skill_flows", None)
    if semantic and len(semantic_pool) < min_occurrences:
        semantic_result["status"] = "insufficient_data"
    elif semantic and callable(cluster_method):
        semantic_result["submitted_flows"] = len(semantic_pool)
        try:
            response = await cluster_method(
                _semantic_flow_records(semantic_pool),
                min_occurrences=min_occurrences,
            )
            if not isinstance(response, dict) or not isinstance(response.get("groups"), list):
                raise ValueError("invalid semantic grouping response")
            semantic_result["status"] = "completed"
            raw_groups = response["groups"][:20]
            semantic_result["returned_groups"] = len(raw_groups)
            by_id = {flow.task_id: flow for flow in semantic_pool}
            claimed_ids: set[str] = set()
            for raw_group in raw_groups:
                if not isinstance(raw_group, dict):
                    semantic_result["rejected_groups"] += 1
                    continue
                raw_ids = raw_group.get("task_ids")
                if not isinstance(raw_ids, list):
                    semantic_result["rejected_groups"] += 1
                    continue
                task_ids = list(dict.fromkeys(str(item) for item in raw_ids))
                if (
                    len(task_ids) < min_occurrences
                    or any(task_id not in by_id for task_id in task_ids)
                    or any(task_id in claimed_ids for task_id in task_ids)
                ):
                    semantic_result["rejected_groups"] += 1
                    continue
                group = [by_id[task_id] for task_id in task_ids]
                compiled = _compile_semantic_group(group)
                if not compiled:
                    semantic_result["rejected_groups"] += 1
                    continue
                label = _clean_model_text(raw_group.get("label"), redactor, 80)
                model_description = _clean_model_text(raw_group.get("description"), redactor, 180)
                rationale = _clean_model_text(raw_group.get("rationale"), redactor, 500)
                candidate, was_created = await _persist_group_candidate(
                    db,
                    group=group,
                    days=days,
                    mode="semantic",
                    redactor=redactor,
                    compiled=compiled,
                    label=label,
                    model_description=model_description,
                    rationale=rationale,
                )
                claimed_ids.update(task_ids)
                semantic_groups += 1
                semantic_result["compiled_groups"] += 1
                (created if was_created else existing).append(candidate)
        except Exception as exc:
            semantic_result["status"] = "failed"
            semantic_result["error_type"] = type(exc).__name__

    return {
        "scanned_tasks": len(flows),
        "repeated_groups": len(exact_groups) + semantic_groups,
        "exact_groups": len(exact_groups),
        "semantic_groups": semantic_groups,
        "created": created,
        "existing": existing,
        "semantic": semantic_result,
        "days": days,
        "min_occurrences": min_occurrences,
    }
