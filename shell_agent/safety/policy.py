"""Environment-aware execution policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shell_agent.safety.classifier import RiskAssessment, RiskLevel
from shell_agent.safety.config import read_safety_yaml


DEFAULT_ENV_POLICIES = {
    "dev": {
        "require_secondary_confirm": False,
        "secondary_confirm_levels": ["critical", "dangerous"],
        "forbidden_executors": [],
    },
    "test": {
        "require_secondary_confirm": False,
        "secondary_confirm_levels": ["critical", "dangerous"],
        "forbidden_executors": [],
    },
    "prod": {
        "require_secondary_confirm": True,
        "secondary_confirm_levels": ["critical", "dangerous"],
        "forbidden_executors": [],
    },
}


@dataclass(frozen=True)
class EnvironmentPolicyResult:
    blocked: bool = False
    block_reason: str = ""
    requires_secondary_confirm: bool = False
    secondary_confirm_expected: str = ""
    secondary_confirm_label: str = ""
    secondary_confirm_reason: str = ""

    def as_payload(self) -> dict:
        return {
            "policy_blocked": self.blocked,
            "policy_block_reason": self.block_reason,
            "requires_secondary_confirm": self.requires_secondary_confirm,
            "secondary_confirm_expected": self.secondary_confirm_expected,
            "secondary_confirm_label": self.secondary_confirm_label,
            "secondary_confirm_reason": self.secondary_confirm_reason,
        }


def evaluate_environment_policy(
    *,
    env: str,
    target: str,
    executor: str,
    risk: RiskAssessment,
    now: datetime | None = None,
) -> EnvironmentPolicyResult:
    """Evaluate per-environment policy for a command preview."""
    env_key = (env or "dev").strip().lower()
    policy = _load_env_policy(env_key)
    forbidden_executors = {
        str(item).strip().lower()
        for item in policy.get("forbidden_executors", [])
        if str(item).strip()
    }
    if executor.strip().lower() in forbidden_executors:
        return EnvironmentPolicyResult(
            blocked=True,
            block_reason=f"{env_key} 环境禁止使用 {executor} 执行器",
        )

    if _time_window_blocks(policy, risk, now=now):
        return EnvironmentPolicyResult(
            blocked=True,
            block_reason=f"{env_key} 环境当前时间不允许执行 {risk.level.value} 命令",
        )

    levels = {
        str(item).strip().lower()
        for item in policy.get("secondary_confirm_levels", ["critical", "dangerous"])
        if str(item).strip()
    }
    requires_secondary = bool(policy.get("require_secondary_confirm")) and risk.level.value in levels
    if not requires_secondary:
        return EnvironmentPolicyResult()

    expected = target or env_key
    return EnvironmentPolicyResult(
        requires_secondary_confirm=True,
        secondary_confirm_expected=expected,
        secondary_confirm_label=f"输入 {expected} 确认",
        secondary_confirm_reason=f"{env_key} 环境的 {risk.level.value} 命令需要二次确认目标别名",
    )


def _load_env_policy(env: str) -> dict:
    policies = {
        key: value.copy()
        for key, value in DEFAULT_ENV_POLICIES.items()
    }
    configured = read_safety_yaml("env_policies.yaml")
    raw_policies = configured.get("environments", configured)
    if isinstance(raw_policies, dict):
        for key, value in raw_policies.items():
            if not isinstance(value, dict):
                continue
            base = policies.get(str(key).lower(), {}).copy()
            base.update(value)
            policies[str(key).lower()] = base
    return policies.get(env, policies["dev"])


def _time_window_blocks(
    policy: dict,
    risk: RiskAssessment,
    now: datetime | None = None,
) -> bool:
    time_window = policy.get("time_window")
    if not isinstance(time_window, dict):
        return False
    allowed = time_window.get(f"{risk.level.value}_allowed")
    if not isinstance(allowed, list) or not allowed:
        return False
    current = (now or datetime.now()).strftime("%H:%M")
    return not any(_time_in_range(current, str(item)) for item in allowed)


def _time_in_range(current: str, window: str) -> bool:
    if "-" not in window:
        return False
    start, end = [part.strip() for part in window.split("-", 1)]
    if not start or not end:
        return False
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end
