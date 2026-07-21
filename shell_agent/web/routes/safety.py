"""Safety policy configuration and classification REST endpoints."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from fastapi import APIRouter

from shell_agent.core.models import AuditRecord
from shell_agent.safety.audit import write_audit
from shell_agent.safety.classifier import RiskLevel, classify_command
from shell_agent.safety.config import SAFETY_CONFIG_DIR, read_safety_yaml
from shell_agent.safety.policy import DEFAULT_ENV_POLICIES, evaluate_environment_policy
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import SafetyClassifyRequest, SafetyConfigUpdate


router = APIRouter()


@router.get("/api/safety/config")
async def get_safety_config() -> dict:
    env_file = read_safety_yaml("env_policies.yaml")
    safe_file = read_safety_yaml("safe_commands.yaml")
    forbidden_file = read_safety_yaml("forbidden_patterns.yaml")
    configured_envs = env_file.get("environments", env_file)
    environments = _merged_env_policies(
        configured_envs if isinstance(configured_envs, dict) else {}
    )
    return {
        "environments": environments,
        "environment_source": "file" if _safety_path("env_policies.yaml").exists() else "default",
        "safe_patterns": _pattern_list_from_config(safe_file),
        "safe_source": "file" if _safety_path("safe_commands.yaml").exists() else "default",
        "forbidden_patterns": _forbidden_rules_from_config(forbidden_file),
        "forbidden_source": "file" if _safety_path("forbidden_patterns.yaml").exists() else "default",
    }


@router.put("/api/safety/config")
async def update_safety_config(update: SafetyConfigUpdate) -> dict:
    rt = get_runtime()
    try:
        environments = _validate_env_policies(update.environments)
        safe_patterns = _validate_safe_patterns(update.safe_patterns)
        forbidden_patterns = _validate_forbidden_patterns(update.forbidden_patterns)

        _write_safety_yaml("env_policies.yaml", {"environments": environments})
        _write_safety_yaml("safe_commands.yaml", {"patterns": safe_patterns})
        _write_safety_yaml("forbidden_patterns.yaml", {"patterns": forbidden_patterns})
        await _write_safety_config_audit(
            rt,
            summary=(
                f"envs={','.join(sorted(environments))}; "
                f"safe_patterns={len(safe_patterns)}; "
                f"forbidden_patterns={len(forbidden_patterns)}"
            ),
        )
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/api/safety/classify")
async def classify_safety_command(request: SafetyClassifyRequest) -> dict:
    risk = classify_command(request.command)
    policy = evaluate_environment_policy(
        env=request.env,
        target=request.target,
        executor=request.executor,
        risk=risk,
    )
    return {"ok": True, **risk.as_payload(), **policy.as_payload()}


def _safety_path(filename: str) -> Path:
    return SAFETY_CONFIG_DIR / filename


def _merged_env_policies(configured: dict) -> dict:
    merged = {key: value.copy() for key, value in DEFAULT_ENV_POLICIES.items()}
    for key, value in configured.items():
        if not isinstance(value, dict):
            continue
        env = str(key).strip().lower()
        base = merged.get(env, {}).copy()
        base.update(value)
        merged[env] = base
    return merged


def _pattern_list_from_config(data: dict) -> list[str]:
    value = data.get("patterns") or data.get("commands") or data.get("rules") or []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            pattern = item.get("pattern") or item.get("command")
        else:
            pattern = item
        pattern = str(pattern or "").strip()
        if pattern:
            result.append(pattern)
    return result


def _forbidden_rules_from_config(data: dict) -> list[dict]:
    value = data.get("patterns") or data.get("rules") or []
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            result.append({
                "name": f"configured_risk_{index}",
                "level": RiskLevel.CRITICAL.value,
                "pattern": item,
                "reason": "命中安全配置中的风险规则",
            })
        elif isinstance(item, dict):
            result.append({
                "name": str(item.get("name") or f"configured_risk_{index}"),
                "level": str(item.get("level") or RiskLevel.CRITICAL.value),
                "pattern": str(item.get("pattern") or ""),
                "reason": str(item.get("reason") or "命中安全配置中的风险规则"),
            })
    return result


def _validate_env_policies(environments: dict) -> dict:
    if not isinstance(environments, dict):
        raise ValueError("环境策略必须是对象")
    result: dict[str, dict] = {}
    allowed_levels = {item.value for item in RiskLevel}
    for raw_env, raw_policy in environments.items():
        env = str(raw_env).strip().lower()
        if not env:
            continue
        if not isinstance(raw_policy, dict):
            raise ValueError(f"{env} 环境策略必须是对象")
        levels = [
            str(item).strip().lower()
            for item in raw_policy.get("secondary_confirm_levels", [])
            if str(item).strip()
        ]
        invalid_levels = [item for item in levels if item not in allowed_levels]
        if invalid_levels:
            raise ValueError(f"{env} 二次确认等级无效: {', '.join(invalid_levels)}")
        policy = {
            "require_secondary_confirm": bool(raw_policy.get("require_secondary_confirm")),
            "secondary_confirm_levels": levels or ["critical", "dangerous"],
            "forbidden_executors": [
                str(item).strip()
                for item in raw_policy.get("forbidden_executors", [])
                if str(item).strip()
            ],
        }
        time_window = raw_policy.get("time_window")
        if isinstance(time_window, dict) and time_window:
            policy["time_window"] = _validate_time_window(env, time_window)
        result[env] = policy
    return result or DEFAULT_ENV_POLICIES


def _validate_time_window(env: str, time_window: dict) -> dict:
    result: dict[str, list[str]] = {}
    for key, value in time_window.items():
        if not isinstance(value, list):
            raise ValueError(f"{env}.{key} 时间窗口必须是列表")
        windows = [str(item).strip() for item in value if str(item).strip()]
        for window in windows:
            if not re.match(r"^\d{2}:\d{2}-\d{2}:\d{2}$", window):
                raise ValueError(f"{env}.{key} 时间窗口格式应为 HH:MM-HH:MM")
        if windows:
            result[str(key)] = windows
    return result


def _validate_safe_patterns(patterns: list[str]) -> list[str]:
    result: list[str] = []
    for pattern in patterns:
        text = str(pattern).strip()
        if not text:
            continue
        re.compile(text, re.I)
        result.append(text)
    return result


def _validate_forbidden_patterns(rules: list[dict]) -> list[dict]:
    result: list[dict] = []
    for index, raw_rule in enumerate(rules, start=1):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"风险规则 #{index} 必须是对象")
        pattern = str(raw_rule.get("pattern") or "").strip()
        if not pattern:
            continue
        re.compile(pattern, re.I)
        try:
            level = RiskLevel(str(raw_rule.get("level") or RiskLevel.CRITICAL.value).lower())
        except ValueError as exc:
            raise ValueError(f"风险规则 #{index} 的等级无效") from exc
        result.append({
            "name": str(raw_rule.get("name") or f"configured_risk_{index}").strip(),
            "level": level.value,
            "pattern": pattern,
            "reason": str(raw_rule.get("reason") or "命中安全配置中的风险规则").strip(),
        })
    return result


def _write_safety_yaml(filename: str, data: dict) -> None:
    SAFETY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _safety_path(filename).open("w", encoding="utf-8") as stream:
        yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)


async def _write_safety_config_audit(rt, summary: str) -> None:
    if not getattr(rt, "db", None):
        return
    record = AuditRecord(
        command="update safety config",
        target="safety-config",
        target_env="local",
        executor="config",
        executed=True,
        source="web",
        caller="web_user",
        session_id="",
        user_confirmed=True,
        stdout=summary,
    )
    await write_audit(rt.db, record)
