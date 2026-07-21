"""Template-skill configuration REST endpoints."""
from __future__ import annotations

from pathlib import Path
import re

import yaml
from fastapi import APIRouter

from shell_agent.core.models import AuditRecord
from shell_agent.safety.audit import write_audit
from shell_agent.skills import load_template_skills
from shell_agent.skills.loader import DEFAULT_TEMPLATE_SKILLS_DIR, parse_template_skill_data
from shell_agent.skills.models import TemplateSkill
from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import SkillYamlUpdate


router = APIRouter()


@router.get("/api/skills")
async def list_skills() -> dict:
    skills = [_skill_payload(skill) for skill in load_template_skills(include_disabled=True)]
    return {"skills": skills}


@router.get("/api/skills/{skill_name}")
async def get_skill(skill_name: str) -> dict:
    skill = _find_template_skill(skill_name)
    if not skill:
        return {"ok": False, "error": f"Skill {skill_name} 不存在"}
    raw_yaml = skill.source_path.read_text(encoding="utf-8")
    return {"ok": True, "skill": _skill_payload(skill, detail=True), "yaml": raw_yaml}


@router.post("/api/skills")
async def create_skill(update: SkillYamlUpdate) -> dict:
    try:
        skill = _parse_skill_yaml(update.yaml)
        path = _skill_path(skill.name)
        if path.exists():
            return {"ok": False, "error": f"Skill {skill.name} 已存在"}
        _write_skill_file(path, update.yaml)
        await _write_skill_config_audit(get_runtime(), "create", skill)
        saved = parse_template_skill_data(_read_yaml(path), path)
        return {"ok": True, "skill": _skill_payload(saved, detail=True)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.put("/api/skills/{skill_name}")
async def update_skill(skill_name: str, update: SkillYamlUpdate) -> dict:
    try:
        current = _find_template_skill(skill_name)
        if not current:
            return {"ok": False, "error": f"Skill {skill_name} 不存在"}
        skill = _parse_skill_yaml(update.yaml)
        if skill.name != skill_name:
            return {"ok": False, "error": "编辑已有 Skill 时不允许修改 name，请新建一个 Skill"}
        _write_skill_file(current.source_path, update.yaml)
        await _write_skill_config_audit(get_runtime(), "update", skill)
        saved = parse_template_skill_data(_read_yaml(current.source_path), current.source_path)
        return {"ok": True, "skill": _skill_payload(saved, detail=True)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.delete("/api/skills/{skill_name}")
async def delete_skill(skill_name: str) -> dict:
    skill = _find_template_skill(skill_name)
    if not skill:
        return {"ok": False, "error": f"Skill {skill_name} 不存在"}
    try:
        skill.source_path.unlink()
        await _write_skill_config_audit(get_runtime(), "delete", skill)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _skill_payload(skill: TemplateSkill, detail: bool = False) -> dict:
    payload = {
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "triggers": skill.triggers,
        "params": [
            {
                "name": param.name,
                "type": param.type,
                "required": param.required,
                "default": param.default,
                "description": param.description,
                "pattern": param.pattern,
                "enum": param.enum,
            }
            for param in skill.params
        ],
        "steps": len(skill.steps),
        "source": str(skill.source_path),
        "enabled": skill.enabled,
    }
    if detail:
        payload["step_items"] = [
            {
                "name": step.name,
                "command": step.command,
                "intent": step.intent,
                "explanation": step.explanation,
                "confirm": step.confirm,
            }
            for step in skill.steps
        ]
        payload["safety"] = skill.safety
    return payload


def _find_template_skill(skill_name: str) -> TemplateSkill | None:
    for skill in load_template_skills(include_disabled=True):
        if skill.name == skill_name:
            return skill
    return None


def _parse_skill_yaml(raw_yaml: str) -> TemplateSkill:
    data = yaml.safe_load(raw_yaml) or {}
    skill = parse_template_skill_data(data, _skill_path(str(data.get("name") or "new_skill")))
    if not re.match(r"^[A-Za-z0-9_-]+$", skill.name):
        raise ValueError("Skill name 只能包含字母、数字、下划线和中划线")
    return skill


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    return data if isinstance(data, dict) else {}


def _skill_path(skill_name: str) -> Path:
    return DEFAULT_TEMPLATE_SKILLS_DIR / f"{skill_name}.yaml"


def _write_skill_file(path: Path, raw_yaml: str) -> None:
    DEFAULT_TEMPLATE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(raw_yaml, encoding="utf-8")


async def _write_skill_config_audit(rt, action: str, skill: TemplateSkill) -> None:
    if not getattr(rt, "db", None):
        return
    record = AuditRecord(
        command=f"{action} template skill {skill.name}",
        target="skill-config",
        target_env="local",
        executor="config",
        executed=True,
        source="web",
        caller="web_user",
        session_id="",
        user_confirmed=True,
        stdout=f"name={skill.name}; steps={len(skill.steps)}; source={skill.source_path}",
    )
    await write_audit(rt.db, record)
