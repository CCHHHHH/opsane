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
    extract: str = ""
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class SkillStep:
    name: str
    command: str
    intent: str = ""
    explanation: str = ""
    confirm: bool = True
    timeout_seconds: int | None = None
    on_failure: str = "abort"


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
    version: str = "1"
    definition_hash: str = ""
