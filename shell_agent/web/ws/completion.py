"""Command and remote-path completion helpers for the WebSocket API."""
from __future__ import annotations

import shlex

from loguru import logger

from shell_agent.web.ws.session_state import (
    _default_target_alias,
    _get_session_context,
)


_COMMON_COMMANDS = sorted(
    {
        "awk", "cat", "cd", "clear", "cp", "curl", "date", "df", "du", "echo",
        "find", "free", "grep", "head", "hostname", "id", "journalctl", "less",
        "ll", "ls", "mkdir", "more", "netstat", "ps", "pwd", "rm", "sed", "ss",
        "systemctl", "tail", "tar", "top", "tree", "uname", "uptime", "vim",
        "vi", "wc", "which", "whoami",
    }
)


def _completion_token(command: str, cursor: int) -> dict:
    before = command[:cursor]
    quote: str | None = None
    escaped = False
    start = 0

    for idx, char in enumerate(before):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if char in ("'", '"'):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            continue
        if quote is None and char.isspace():
            start = idx + 1

    raw_token = before[start:]
    token_start = start
    if raw_token[:1] in ("'", '"'):
        raw_token = raw_token[1:]
        token_start += 1

    prefix_before_token = before[:start].strip()
    return {
        "prefix": raw_token,
        "start": token_start,
        "end": cursor,
        "kind": "command" if not prefix_before_token else "path",
    }


def _complete_builtin_command(prefix: str) -> list[str]:
    prefix_lower = prefix.lower()
    return [command for command in _COMMON_COMMANDS if command.startswith(prefix_lower)]


async def _complete_remote_path(
    rt,
    session_id: str,
    target: str,
    prefix: str,
) -> list[str]:
    target_alias = target or _default_target_alias(rt)
    if not target_alias:
        return []

    context = _get_session_context(rt, session_id)
    cwd = context.get_cwd(target_alias)
    completion_script = (
        f"compgen -f -- {shlex.quote(prefix)} | head -n 80 | "
        "while IFS= read -r p; do "
        'if [ -d "$p" ]; then printf "%s/\\n" "$p"; else printf "%s\\n" "$p"; fi; '
        "done"
    )
    actual = f"cd {shlex.quote(cwd)} && {completion_script}" if cwd else completion_script
    try:
        command = rt.executor.normalize(f"ssh {target_alias} {actual!r}")
        command.source = "completion"
        result = await rt.executor.execute(command)
    except Exception as exc:
        logger.debug(f"路径补全失败: {exc}")
        return []

    if result.timed_out or result.exit_code not in (0, 1):
        return []
    return _unique([line.strip() for line in result.stdout.splitlines() if line.strip()])


def _common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        while prefix and not value.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
