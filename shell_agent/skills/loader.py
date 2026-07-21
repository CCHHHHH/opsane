"""Load Template Skills from YAML files."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from shell_agent.skills.models import SkillParam, SkillStep, TemplateSkill


DEFAULT_TEMPLATE_SKILLS_DIR = Path("skills/templates")


def load_template_skills(
    path: str | Path | None = None,
    *,
    include_disabled: bool = False,
) -> list[TemplateSkill]:
    """Load enabled template skills.

    Invalid files are skipped with a log entry so one broken draft skill does
    not prevent the application from starting.
    """
    root = _default_template_skills_dir() if path is None else Path(path)
    if not root.exists():
        return []

    skills: list[TemplateSkill] = []
    for file_path in sorted(root.glob("*.yaml")):
        try:
            skill = load_template_skill_file(file_path)
        except Exception as e:
            logger.warning(f"加载 Skill 失败: {file_path} error={e}")
            continue
        if include_disabled or skill.enabled:
            skills.append(skill)
    return skills


def _default_template_skills_dir() -> Path:
    """Resolve source-tree skills before falling back to installed data."""
    if DEFAULT_TEMPLATE_SKILLS_DIR.exists():
        return DEFAULT_TEMPLATE_SKILLS_DIR
    return Path(sys.prefix) / "skills" / "templates"


def load_template_skill_file(path: Path) -> TemplateSkill:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return parse_template_skill_data(data, path)


def parse_template_skill_data(data: dict[str, Any], path: Path) -> TemplateSkill:
    if not isinstance(data, dict):
        raise ValueError("Skill YAML 顶层必须是对象")

    steps = [_parse_step(item) for item in data.get("steps", []) if isinstance(item, dict)]
    if not steps:
        raise ValueError("Skill 至少需要一个 step")

    return TemplateSkill(
        name=str(data.get("name") or path.stem),
        description=str(data.get("description") or ""),
        category=str(data.get("category") or "general"),
        triggers=[str(item) for item in data.get("triggers", []) if str(item).strip()],
        params=[
            _parse_param(item)
            for item in data.get("params", [])
            if isinstance(item, dict) and item.get("name")
        ],
        steps=steps,
        source_path=path,
        enabled=bool(data.get("enabled", True)),
        safety=data.get("safety") if isinstance(data.get("safety"), dict) else {},
    )


def _parse_param(item: dict[str, Any]) -> SkillParam:
    enum = item.get("enum", [])
    return SkillParam(
        name=str(item.get("name")),
        type=str(item.get("type") or "string"),
        required=bool(item.get("required")),
        default=item.get("default"),
        description=str(item.get("description") or ""),
        pattern=str(item.get("pattern") or ""),
        enum=[str(value) for value in enum] if isinstance(enum, list) else [],
    )


def _parse_step(item: dict[str, Any]) -> SkillStep:
    command = str(item.get("command") or "").strip()
    if not command:
        raise ValueError("step.command 不能为空")
    return SkillStep(
        name=str(item.get("name") or "执行命令"),
        command=command,
        intent=str(item.get("intent") or item.get("name") or "执行 Skill 步骤"),
        explanation=str(item.get("explanation") or ""),
        confirm=bool(item.get("confirm", True)),
    )
