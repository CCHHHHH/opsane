"""Template Skill data structures."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillParam:
    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    description: str = ""
    pattern: str = ""
    enum: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillStep:
    name: str
    command: str
    intent: str = ""
    explanation: str = ""
    confirm: bool = True


@dataclass(frozen=True)
class TemplateSkill:
    name: str
    description: str
    category: str
    triggers: list[str]
    params: list[SkillParam]
    steps: list[SkillStep]
    source_path: Path
    enabled: bool = True
    safety: dict[str, Any] = field(default_factory=dict)
