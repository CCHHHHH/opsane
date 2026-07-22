"""核心数据结构"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class InputType(str, Enum):
    AUTO = "auto"
    COMMAND = "command"
    NATURAL = "natural"


class ConfirmMode(str, Enum):
    INTERACTIVE = "interactive"
    AUTO_SAFE = "auto_safe"
    FULL_ACCESS = "full_access"
    DRY_RUN = "dry_run"


@dataclass
class AgentRequest:
    """四个入口统一转换的内部请求模型"""
    input: str
    input_type: InputType | str = InputType.AUTO
    target: str | None = None
    session_id: str = field(default_factory=lambda: _gen_id("sess"))
    caller: str = "anonymous"
    source: str = "cli"  # cli | mcp | http | webhook
    timestamp: str = field(default_factory=_now_iso)

    def detect_input_type(self) -> InputType:
        """auto 模式下用轻量规则判断 input 类型"""
        # 字符串转 enum
        if isinstance(self.input_type, str):
            self.input_type = InputType(self.input_type)
        if self.input_type != InputType.AUTO:
            return self.input_type
        text = self.input.strip().lower()
        # 以 ssh/mysql/kubectl/docker 等开头，且含空格分词 → command
        command_prefixes = ("ssh ", "mysql ", "kubectl ", "docker ", "systemctl ",
                           "ps ", "df ", "top ", "tail ", "cat ", "grep ", "curl ")
        if any(text.startswith(p) for p in command_prefixes):
            return InputType.COMMAND
        return InputType.NATURAL


@dataclass
class PendingCommand:
    """命令规范化后的对象"""
    raw: str
    target: str
    target_env: str
    executor: str
    actual_command: str
    source: str = "direct"  # direct | llm
    user_input: str = ""
    intent: str = ""
    explanation: str = ""
    response_mode: str = "raw"  # raw | workflow | collect | analyze | investigate
    confirm_mode: str = "interactive"
    display_command: str = ""
    cwd_update: bool = False
    step_index: int = 1
    max_steps: int = 0  # 0 means LLM-driven without a fixed product step limit.
    step_queue: list[dict[str, Any]] = field(default_factory=list)
    skill_name: str | None = None
    step_name: str | None = None
    skill_version: str = ""
    skill_hash: str = ""
    skill_default_confirm_mode: str = "interactive"
    skill_force_confirm: bool = False
    skill_on_failure: str = "abort"
    skill_had_failures: bool = False
    timeout_seconds: int | None = None
    policy_blocked: bool = False
    policy_block_reason: str = ""
    requires_secondary_confirm: bool = False
    secondary_confirm_expected: str = ""
    secondary_confirm_label: str = ""
    secondary_confirm_reason: str = ""
    task_id: str = ""
    id: str = field(default_factory=lambda: _gen_id("cmd"))


@dataclass
class ExecutionResult:
    """命令执行结果"""
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False


@dataclass
class AuditRecord:
    """审计记录"""
    command: str
    target: str
    target_env: str
    executor: str
    executed: bool
    source: str
    caller: str
    session_id: str
    user_confirmed: bool | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    truncated: bool = False
    timed_out: bool = False
    timestamp: str = field(default_factory=_now_iso)
    id: str = field(default_factory=lambda: _gen_id("aud"))
