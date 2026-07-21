"""静态命令风险分类器。

分类器只基于命令文本做保守的启发式判断，不尝试理解完整 shell 语义。
由于 ``auto_safe`` 会自动执行 ``safe`` 命令，安全规则必须默认拒绝未知的
复合 shell 结构。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from shell_agent.safety.config import read_safety_list


class RiskLevel(str, Enum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"
    CRITICAL = "critical"


_RISK_ORDER = {
    RiskLevel.SAFE: 0,
    RiskLevel.CAUTION: 1,
    RiskLevel.DANGEROUS: 2,
    RiskLevel.CRITICAL: 3,
}


@dataclass
class RiskAssessment:
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "risk_level": self.level.value,
            "risk_reasons": self.reasons,
            "risk_rules": self.rules,
        }


@dataclass(frozen=True)
class _Rule:
    name: str
    level: RiskLevel
    pattern: re.Pattern[str]
    reason: str


_RULES = [
    _Rule(
        "rm_recursive",
        RiskLevel.CRITICAL,
        re.compile(r"(^|[;&|]\s*)rm\s+[^;&|]*-(?:[^\s;&|]*r[^\s;&|]*f|[^\s;&|]*f[^\s;&|]*r)\b", re.I),
        "递归强制删除可能造成不可恢复的数据丢失",
    ),
    _Rule(
        "filesystem_format",
        RiskLevel.CRITICAL,
        re.compile(r"\b(mkfs|mke2fs|wipefs)\b|\bdd\b[^;&|]*\bof=", re.I),
        "格式化或块设备写入属于高破坏性操作",
    ),
    _Rule(
        "shutdown_reboot",
        RiskLevel.CRITICAL,
        re.compile(r"\b(shutdown|reboot|poweroff|halt)\b", re.I),
        "关机或重启会中断服务",
    ),
    _Rule(
        "sql_drop_truncate",
        RiskLevel.CRITICAL,
        re.compile(r"\b(drop|truncate)\s+(table|database|schema)\b", re.I),
        "DROP/TRUNCATE 会删除数据库对象或数据",
    ),
    _Rule(
        "redis_flush",
        RiskLevel.CRITICAL,
        re.compile(r"\bflush(all|db)\b", re.I),
        "Redis flush 操作会清空缓存或数据",
    ),
    _Rule(
        "rm_delete",
        RiskLevel.DANGEROUS,
        re.compile(r"(^|[;&|]\s*)rm\s+", re.I),
        "删除文件会改变目标机器状态",
    ),
    _Rule(
        "file_truncate",
        RiskLevel.DANGEROUS,
        re.compile(r"(^|[;&|]\s*)truncate\s+", re.I),
        "截断文件会清空或改变目标机器上的文件内容",
    ),
    _Rule(
        "service_mutation",
        RiskLevel.DANGEROUS,
        re.compile(r"\bsystemctl\s+(restart|stop|start|reload|disable|enable)\b|\bservice\s+\S+\s+(restart|stop|start|reload)\b", re.I),
        "服务启停或重载会改变运行状态",
    ),
    _Rule(
        "container_mutation",
        RiskLevel.DANGEROUS,
        re.compile(r"\bdocker\s+(rm|rmi|restart|stop|kill|compose\s+down)\b", re.I),
        "容器删除、停止或重启会影响服务",
    ),
    _Rule(
        "kubernetes_mutation",
        RiskLevel.DANGEROUS,
        re.compile(r"\bkubectl\s+(delete|apply|replace|scale|rollout\s+restart|cordon|drain)\b", re.I),
        "Kubernetes 变更命令会影响集群资源",
    ),
    _Rule(
        "process_signal",
        RiskLevel.DANGEROUS,
        re.compile(r"(^|[;&|]\s*)kill(all)?\s+", re.I),
        "发送进程信号可能中断服务",
    ),
    _Rule(
        "permission_change",
        RiskLevel.DANGEROUS,
        re.compile(r"\b(chmod|chown|chgrp)\b", re.I),
        "权限或属主变更可能影响访问控制",
    ),
    _Rule(
        "package_install",
        RiskLevel.DANGEROUS,
        re.compile(r"\b(apt|apt-get|yum|dnf|apk|brew)\s+(install|remove|upgrade|update)\b", re.I),
        "包管理操作会改变系统软件状态",
    ),
    _Rule(
        "file_write",
        RiskLevel.DANGEROUS,
        re.compile(
            r"(^|[^<>])>\s*(?!/dev/null(?:\s|$|[;&|)]))\S+|\btee\s+",
            re.I,
        ),
        "重定向或 tee 写文件会改变目标机器状态",
    ),
    _Rule(
        "broad_read",
        RiskLevel.CAUTION,
        re.compile(r"(^|[;&|]\s*)(cat|find|du)\b", re.I),
        "读取范围可能较大，注意输出量和敏感信息",
    ),
    _Rule(
        "network_request",
        RiskLevel.CAUTION,
        re.compile(r"\bcurl\b(?![^;&|]*\s(-I|--head)\b)", re.I),
        "网络请求可能触发远端副作用或暴露信息",
    ),
]


_SAFE_PATTERN = re.compile(
    r"^\s*(df|uptime|free|ps|top|whoami|id|hostname|date|pwd|cd|ls|tail|head|grep|"
    r"journalctl|ss|netstat|curl\s+(-I|--head)\b)\b",
    re.I,
)


_KNOWN_READ_ONLY_COMPOUND_PATTERNS = [
    re.compile(
        r'''^cd\s+(?:[A-Za-z0-9._@%+=,:~/-]+|'[^'\r\n]*'|"[^"$`\\\r\n]*")\s*&&\s*pwd$''',
        re.I,
    ),
    re.compile(
        r"^ps\s+-ef\s*\|\s*grep\s+java\s*\|\s*grep\s+-v\s+grep$",
        re.I,
    ),
    re.compile(
        r'''^uptime\s*&&\s*echo\s+(?:---|'---'|"---")\s*&&\s*free\s+-h'''
        r'''\s*&&\s*echo\s+(?:---|'---'|"---")\s*&&\s*df\s+-h$''',
        re.I,
    ),
    re.compile(
        r'''^tail\s+-n\s+[1-9]\d{0,4}\s+"\$\(ls\s+-t\s+'''
        r'''(?P<log_dir>/[A-Za-z0-9._@%+=,:~/-]*)/\*\.log\s+'''
        r'''(?P=log_dir)/\*\.out\s+2>\s*/dev/null\s*\|\s*head\s+-n\s+1\)"$''',
        re.I,
    ),
]


def classify_command(command: str) -> RiskAssessment:
    """Classify a shell command into a conservative static risk level."""
    stripped = command.strip()
    normalized = " ".join(stripped.split())
    if not normalized:
        return RiskAssessment(
            level=RiskLevel.CAUTION,
            reasons=["空命令无法判断风险"],
            rules=["empty_command"],
        )

    matched: list[_Rule] = []
    for rule in [*_RULES, *_configured_risk_rules()]:
        if rule.pattern.search(normalized):
            matched.append(rule)

    sql_assessment = _classify_sql_mutation(normalized)
    if sql_assessment:
        matched.append(sql_assessment)

    if matched:
        level = max((rule.level for rule in matched), key=lambda item: _RISK_ORDER[item])
        return RiskAssessment(
            level=level,
            reasons=_unique([rule.reason for rule in matched]),
            rules=[rule.name for rule in matched],
        )

    if _has_compound_shell_syntax(stripped):
        if _matches_known_read_only_compound(normalized):
            return RiskAssessment(
                level=RiskLevel.SAFE,
                reasons=["匹配内置的只读复合命令"],
                rules=["known_read_only"],
            )
        return RiskAssessment(
            level=RiskLevel.CAUTION,
            reasons=["包含未明确放行的复合 Shell 语法，需要人工确认"],
            rules=["compound_shell_command"],
        )

    if _SAFE_PATTERN.search(normalized) or any(
        pattern.search(normalized) for pattern in _configured_safe_patterns()
    ):
        return RiskAssessment(
            level=RiskLevel.SAFE,
            reasons=["匹配常见只读排查命令"],
            rules=["known_read_only"],
        )

    return RiskAssessment(
        level=RiskLevel.CAUTION,
        reasons=["未命中明确安全规则，建议人工确认命令意图"],
        rules=["unknown_command"],
    )


def _has_compound_shell_syntax(command: str) -> bool:
    """Detect shell control syntax outside literal quotes.

    This is intentionally a fail-closed scanner, not a complete shell parser.
    Operators inside single quotes are data. Command substitution remains active
    inside double quotes and is therefore treated as compound syntax.
    """
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue

        if quote == "'":
            if char == "'":
                quote = ""
            index += 1
            continue

        if char == "\\":
            escaped = True
            index += 1
            continue

        if quote == '"':
            if char == '"':
                quote = ""
            elif char == "`" or command.startswith("$(", index):
                return True
            index += 1
            continue

        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char in ";|&>\n`":
            return True
        if command.startswith("$(", index):
            return True
        if command.startswith("<(", index) or command.startswith(">(", index):
            return True
        index += 1
    return False


def _matches_known_read_only_compound(command: str) -> bool:
    return any(pattern.fullmatch(command) for pattern in _KNOWN_READ_ONLY_COMPOUND_PATTERNS)


def _classify_sql_mutation(command: str) -> _Rule | None:
    lower = command.lower()
    if re.search(r"\bdelete\s+from\b", lower):
        if not re.search(r"\bwhere\b", lower):
            return _synthetic_rule(
                "sql_delete_without_where",
                RiskLevel.CRITICAL,
                "DELETE 无 WHERE 可能删除整表数据",
            )
        return _synthetic_rule(
            "sql_delete_with_where",
            RiskLevel.DANGEROUS,
            "DELETE 会修改数据库数据",
        )
    if re.search(r"\bupdate\s+\S+\s+set\b", lower):
        if not re.search(r"\bwhere\b", lower):
            return _synthetic_rule(
                "sql_update_without_where",
                RiskLevel.CRITICAL,
                "UPDATE 无 WHERE 可能更新整表数据",
            )
        return _synthetic_rule(
            "sql_update_with_where",
            RiskLevel.DANGEROUS,
            "UPDATE 会修改数据库数据",
        )
    return None


def _synthetic_rule(name: str, level: RiskLevel, reason: str) -> _Rule:
    return _Rule(name, level, re.compile(r"$."), reason)


def _configured_risk_rules() -> list[_Rule]:
    entries = read_safety_list("forbidden_patterns.yaml", ("patterns", "rules"))
    rules: list[_Rule] = []
    for index, entry in enumerate(entries, start=1):
        if isinstance(entry, str):
            item = {"pattern": entry}
        elif isinstance(entry, dict):
            item = entry
        else:
            continue
        pattern = str(item.get("pattern", "")).strip()
        if not pattern:
            continue
        try:
            level = RiskLevel(str(item.get("level", RiskLevel.CRITICAL.value)).lower())
        except ValueError:
            level = RiskLevel.CRITICAL
        try:
            compiled = re.compile(pattern, re.I)
        except re.error:
            continue
        rules.append(
            _Rule(
                str(item.get("name") or f"configured_risk_{index}"),
                level,
                compiled,
                str(item.get("reason") or "命中安全配置中的风险规则"),
            )
        )
    return rules


def _configured_safe_patterns() -> list[re.Pattern[str]]:
    entries = read_safety_list("safe_commands.yaml", ("patterns", "commands", "rules"))
    patterns: list[re.Pattern[str]] = []
    for entry in entries:
        pattern = entry
        if isinstance(entry, dict):
            pattern = entry.get("pattern") or entry.get("command")
        pattern = str(pattern or "").strip()
        if not pattern:
            continue
        try:
            patterns.append(re.compile(pattern, re.I))
        except re.error:
            continue
    return patterns


def _unique(items: list[str]) -> list[str]:
    seen = set()
    unique_items = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique_items.append(item)
    return unique_items
