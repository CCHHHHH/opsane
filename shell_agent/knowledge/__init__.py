"""Knowledge extraction and resolution for Shell Agent."""

from shell_agent.knowledge.learning import LearningResult, learn_from_task
from shell_agent.knowledge.resolver import KnowledgeResolution, KnowledgeResolver

__all__ = [
    "KnowledgeResolution",
    "KnowledgeResolver",
    "LearningResult",
    "learn_from_task",
]
