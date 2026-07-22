"""Template Skill matching and rendering."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from string import Template
from typing import Any

from shell_agent.skills.loader import load_template_skills
from shell_agent.skills.models import SkillParam, TemplateSkill


@dataclass(frozen=True)
class SkillMatch:
    skill: TemplateSkill
    params: dict[str, Any]
    steps: list[dict[str, Any]]
    missing_params: list[str] = field(default_factory=list)


def match_template_skill(
    user_input: str,
    *,
    server_aliases: list[str],
    default_target: str = "",
    skills: list[TemplateSkill] | None = None,
) -> SkillMatch | None:
    """Match a user utterance to an enabled Template Skill."""
    text = user_input.strip()
    if not text:
        return None

    candidates = skills if skills is not None else load_template_skills()
    scored: list[tuple[int, TemplateSkill]] = []
    for skill in candidates:
        score = _trigger_score(text, skill.triggers)
        if score > 0:
            scored.append((score, skill))
    if not scored:
        return None

    scored.sort(key=lambda item: (-item[0], item[1].name))
    for _, skill in scored:
        params, missing = _extract_params(skill, text, server_aliases, default_target)
        if missing:
            return SkillMatch(skill=skill, params=params, steps=[], missing_params=missing)
        return SkillMatch(skill=skill, params=params, steps=_render_steps(skill, params))
    return None


def _trigger_score(text: str, triggers: list[str]) -> int:
    lower = text.lower()
    score = 0
    for trigger in triggers:
        item = trigger.strip()
        if not item:
            continue
        if item.startswith("re:"):
            try:
                if re.search(item[3:], text, re.I):
                    score = max(score, 30 + len(item))
            except re.error:
                continue
        elif item.lower() in lower:
            score = max(score, 10 + len(item))
    return score


def _extract_params(
    skill: TemplateSkill,
    text: str,
    server_aliases: list[str],
    default_target: str,
) -> tuple[dict[str, Any], list[str]]:
    values = {
        "user_input": text,
        "target": _extract_targets(text, server_aliases, default_target)[0],
        "targets": _extract_targets(text, server_aliases, default_target),
        "path": _extract_path(text),
        "lines": _extract_lines(text) or 200,
    }
    missing: list[str] = []
    for param in skill.params:
        value = values.get(param.name)
        if _is_empty(value) and param.extract:
            match = re.search(param.extract, text, re.I)
            if match:
                value = match.groupdict().get(param.name) or (
                    match.group(1) if match.lastindex else match.group(0)
                )
        if _is_empty(value) and param.default is not None:
            value = param.default
        try:
            value = _coerce_param(param, value, server_aliases)
        except ValueError:
            value = None
        if param.required and _is_empty(value):
            missing.append(param.name)
        values[param.name] = value
    return values, missing


def _extract_targets(text: str, server_aliases: list[str], default_target: str) -> list[str]:
    lower = text.lower()
    targets: list[str] = []
    for alias in sorted(server_aliases, key=len, reverse=True):
        if alias.lower() in lower:
            targets.append(alias)
    if targets:
        return targets
    fallback = default_target or (server_aliases[0] if server_aliases else "")
    return [fallback] if fallback else [""]


def _extract_path(text: str) -> str:
    match = re.search(r"(?<!\S)(/[A-Za-z0-9._@:%+=,~/-]+)", text)
    if not match:
        return ""
    return match.group(1).rstrip("，。；;、")


def _extract_lines(text: str) -> int | None:
    match = re.search(r"(\d{1,5})\s*(行|lines?)", text, re.I)
    if not match:
        return None
    value = int(match.group(1))
    return max(1, min(value, 5000))


def _render_steps(skill: TemplateSkill, params: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    param_specs = {param.name: param for param in skill.params}
    targets = params.get("targets") if isinstance(params.get("targets"), list) else [params.get("target")]
    render_targets = targets if any("{{target}}" in step.command for step in skill.steps) else [params.get("target")]
    for target in render_targets:
        scoped_params = {**params, "target": target}
        display_params = {
            key: "" if value is None or isinstance(value, list) else str(value)
            for key, value in scoped_params.items()
        }
        for step in skill.steps:
            steps.append(
                {
                    "command": _render_command_template(step.command, scoped_params, param_specs),
                    "intent": _render_template(step.intent, display_params),
                    "explanation": _render_template(step.explanation, display_params),
                    "skill_step_name": step.name,
                    "confirm": step.confirm,
                    "timeout_seconds": step.timeout_seconds,
                    "on_failure": step.on_failure,
                    "skill_name": skill.name,
                    "skill_version": skill.version,
                    "skill_hash": skill.definition_hash,
                    "skill_default_confirm_mode": str(
                        skill.safety.get("default_confirm_mode") or "interactive"
                    ),
                }
            )
    return steps


def _render_template(template: str, params: dict[str, str]) -> str:
    text = template
    for key in params:
        text = text.replace("{{" + key + "}}", "${" + key + "}")
    return Template(text).safe_substitute(params)


_PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._@:%+=,~/-]+$")
_SAFE_SERVER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _coerce_param(param: SkillParam, value: Any, server_aliases: list[str]) -> Any:
    if _is_empty(value):
        return value
    if param.type == "integer":
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"参数 {param.name} 必须是整数") from exc
        if param.minimum is not None and value < param.minimum:
            raise ValueError(f"参数 {param.name} 小于允许值")
        if param.maximum is not None and value > param.maximum:
            raise ValueError(f"参数 {param.name} 超过允许值")
    elif param.type == "boolean":
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "是"}:
            value = True
        elif normalized in {"false", "0", "no", "否"}:
            value = False
        else:
            raise ValueError(f"参数 {param.name} 必须是布尔值")
    else:
        value = str(value).strip()
    if param.type == "server_alias":
        canonical = next((alias for alias in server_aliases if alias.lower() == value.lower()), None)
        if not canonical or not _SAFE_SERVER_RE.fullmatch(canonical):
            raise ValueError(f"参数 {param.name} 不是已登记服务器")
        value = canonical
    elif param.type == "shell_path":
        if not _SAFE_PATH_RE.fullmatch(value) or ".." in value.split("/"):
            raise ValueError(f"参数 {param.name} 不是安全绝对路径")
    if param.enum and str(value) not in param.enum:
        raise ValueError(f"参数 {param.name} 不在允许范围")
    if param.pattern and value is not None and not re.search(param.pattern, str(value)):
        raise ValueError(f"参数 {param.name} 格式不合法")
    return value


def _render_command_template(
    template: str,
    params: dict[str, Any],
    specs: dict[str, SkillParam],
) -> str:
    rendered = template
    for name in _PLACEHOLDER_RE.findall(template):
        spec = specs.get(name)
        if spec is None:
            raise ValueError(f"命令模板引用了未声明参数: {name}")
        value = params.get(name)
        if _is_empty(value):
            raise ValueError(f"命令模板参数为空: {name}")
        if spec.type == "string" and not spec.enum:
            raise ValueError(f"普通字符串参数 {name} 不能直接插入命令，请使用受约束类型")
        if spec.type == "shell_arg":
            command_value = shlex.quote(str(value))
        elif spec.type == "boolean":
            command_value = "true" if value else "false"
        else:
            command_value = str(value)
        rendered = re.sub(
            r"{{\s*" + re.escape(name) + r"\s*}}",
            lambda _: command_value,
            rendered,
        )
    if _PLACEHOLDER_RE.search(rendered):
        raise ValueError("命令模板仍包含未解析参数")
    return rendered


def _is_empty(value: Any) -> bool:
    return value is None or value == ""
