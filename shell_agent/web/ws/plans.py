"""Pure operation-plan normalization and payload helpers."""
from __future__ import annotations

import shlex
from typing import Any
from uuid import uuid4

from shell_agent.safety.classifier import RiskLevel, classify_command
from shell_agent.web.ws.transport import _SEND_SESSION_ID, _SEND_TURN_ID


def _pending_plan_key(session_id: str) -> str:
    return f"{session_id}:chat"


def _is_operation_plan_result(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    return (
        result.get("type") == "operation_plan"
        or result.get("response_mode") == "operation_plan"
        or isinstance(result.get("plan"), dict)
    )


def _normalize_operation_plan(result: dict, user_input: str, confirm_mode: str) -> dict:
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else result
    plan_id = str(result.get("plan_id") or plan.get("plan_id") or f"plan_{uuid4().hex[:12]}")
    return {
        "plan_id": plan_id,
        "channel": "chat",
        "user_input": user_input,
        "confirm_mode": confirm_mode,
        "intent": str(result.get("intent") or plan.get("intent") or plan.get("title") or "操作方案"),
        "title": str(plan.get("title") or result.get("intent") or "操作方案"),
        "goal": str(plan.get("goal") or user_input),
        "recommended_approach": str(
            plan.get("recommended_approach")
            or plan.get("approach")
            or plan.get("summary")
            or ""
        ),
        "impact": _as_str_list(plan.get("impact")),
        "risks": _as_str_list(plan.get("risks")),
        "rollback": _as_str_list(plan.get("rollback")),
        "verification": _as_str_list(plan.get("verification")),
        "steps": _planned_steps_from_result(plan) or _planned_steps_from_result(result),
        "raw": result,
    }


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _operation_plan_payload(plan: dict, active: bool = False) -> dict:
    return {
        "session_id": _SEND_SESSION_ID.get(),
        "turn_id": plan.get("turn_id") or _SEND_TURN_ID.get(),
        "channel": "chat",
        "plan_id": plan.get("plan_id", ""),
        "intent": plan.get("intent", ""),
        "title": plan.get("title", ""),
        "goal": plan.get("goal", ""),
        "recommended_approach": plan.get("recommended_approach", ""),
        "impact": plan.get("impact", []),
        "risks": plan.get("risks", []),
        "rollback": plan.get("rollback", []),
        "verification": plan.get("verification", []),
        "steps": plan.get("steps", []),
        "active": active,
    }


def _get_pending_operation_plan(rt, session_id: str, plan_id: str = "") -> dict | None:
    plan = getattr(rt, "pending_operation_plans", {}).get(_pending_plan_key(session_id))
    if not plan:
        return None
    if plan_id and plan.get("plan_id") != plan_id:
        return None
    return plan


def _should_force_operation_plan(result: dict, planned_steps: list[dict[str, str]]) -> bool:
    """Safety fallback when LLM forgets to return operation_plan for mutations."""
    steps = planned_steps or []
    if not steps and result.get("command"):
        steps = [{"command": str(result.get("command", ""))}]
    for step in steps:
        command_text = _actual_command_for_risk(step.get("command", ""))
        risk = classify_command(command_text)
        if risk.level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL):
            return True
    return False


def _actual_command_for_risk(command: str) -> str:
    text = (command or "").strip()
    if not text.lower().startswith("ssh "):
        return text
    try:
        return shlex.split(text)[-1]
    except ValueError:
        return text


def _target_from_ssh_command(command: str) -> str:
    text = (command or "").strip()
    if not text.lower().startswith("ssh "):
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        return ""
    return parts[1] if len(parts) > 1 else ""


def _operation_plan_from_command_result(
    result: dict,
    planned_steps: list[dict[str, str]],
    user_input: str,
) -> dict:
    steps = planned_steps or []
    if not steps and result.get("command"):
        steps = [
            {
                "command": str(result.get("command", "")).strip(),
                "target": _target_from_ssh_command(str(result.get("command", "")).strip()),
                "intent": str(result.get("intent", "")).strip(),
                "explanation": str(result.get("explanation", "")).strip(),
            }
        ]
    impact: list[str] = []
    risks: list[str] = []
    for step in steps:
        command_text = _actual_command_for_risk(step.get("command", ""))
        target = step.get("target") or _target_from_ssh_command(step.get("command", ""))
        risk = classify_command(command_text)
        impact.append(f"将在 {target or '未明确目标'} 执行: {step.get('intent') or command_text}")
        risks.extend(risk.reasons)
    return {
        "type": "operation_plan",
        "intent": str(result.get("intent") or "执行会修改服务器状态的操作"),
        "response_mode": "operation_plan",
        "plan": {
            "title": str(result.get("intent") or "待确认操作方案"),
            "goal": user_input,
            "recommended_approach": (
                "该请求会修改服务器状态，系统已将 LLM 生成的命令转为方案确认。"
                "请确认影响、风险和步骤后再进入命令预览。"
            ),
            "impact": _unique_strings(impact),
            "risks": _unique_strings(risks) or ["该操作会改变目标服务器状态"],
            "rollback": ["如操作写入配置或修改文件，应先确认备份；执行后按变更内容恢复原文件或配置。"],
            "verification": ["执行后根据命令输出和目标文件/服务状态验证是否达到预期。"],
            "steps": steps,
        },
    }


def _unique_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def _planned_steps_from_result(result: dict) -> list[dict[str, str]]:
    raw_steps = result.get("steps", [])
    if not isinstance(raw_steps, list):
        return []
    steps: list[dict[str, str]] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command", "")).strip()
        if not command:
            continue
        step = {
            "command": command,
            "intent": str(item.get("intent", "")).strip(),
            "explanation": str(item.get("explanation", "")).strip(),
        }
        explicit_target = str(item.get("target") or "").strip()
        if explicit_target:
            step["target"] = explicit_target
        steps.append(step)
    return steps
