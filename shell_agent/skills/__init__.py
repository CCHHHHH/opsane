"""Skill engine package."""

from shell_agent.skills.engine import SkillMatch, match_template_skill
from shell_agent.skills.loader import load_template_skills

__all__ = ["SkillMatch", "load_template_skills", "match_template_skill"]
