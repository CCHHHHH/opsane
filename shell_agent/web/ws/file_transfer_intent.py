"""Deterministic resolution of conversational session-file transfers.

The LLM must not invent a local file, SSH target, or destination path for a
write operation.  This resolver only returns an executable preview when all
three objects are explicit and unambiguous in the current session.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from shell_agent.storage.session_files import list_session_files


_ACTION_RE = re.compile(
    r"(?:上传|传到|传至|传输|发送|推送|拷贝|复制|放到|放入|放进|放至|置于)"
)
_FILE_HINT_RE = re.compile(
    r"(?:文件|附件|安装包|部署包|制品|包|jar|war|zip|tar|tgz|rpm|deb|7z)",
    re.I,
)
_REMOTE_HINT_RE = re.compile(r"(?:服务器|主机|机器|远端|目录|路径|到|至)")
_RECENT_UPLOAD_RE = re.compile(r"刚(?:才)?上传的")
_DEPLOYMENT_RE = re.compile(
    r"(?:部署(?!包)|发布|发版|上线|替换|安装(?!包)|重启|启动|回滚)"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(/[^　\s，。；、,;]+)")
_TRAILING_PATH_PUNCTUATION = "'\"`>)]}】》：:。；，,;！!？?"
_REMOTE_PATH_DESCRIPTION_RE = re.compile(r"(?:目录|文件夹|路径)(?:下|中|里)?$")
_KNOWN_FILE_EXTENSIONS = (
    "tar.bz2",
    "tar.gz",
    "tar.xz",
    "jar",
    "war",
    "zip",
    "tar",
    "tgz",
    "rpm",
    "deb",
    "7z",
    "gz",
)


@dataclass(frozen=True)
class ConversationalTransferIntent:
    file_id: str
    file_name: str
    target: str
    remote_dir: str
    overwrite: bool


@dataclass(frozen=True)
class TransferIntentResolution:
    attempted: bool
    intent: ConversationalTransferIntent | None = None
    clarification: str = ""


def _alias_occurs(message: str, alias: str) -> bool:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_.-]){re.escape(alias)}(?![A-Za-z0-9_.-])",
        re.I,
    )
    return pattern.search(message) is not None


def _extract_absolute_paths(message: str) -> list[str]:
    paths: list[str] = []
    for raw in _ABSOLUTE_PATH_RE.findall(message):
        value = raw.rstrip(_TRAILING_PATH_PUNCTUATION)
        value = _REMOTE_PATH_DESCRIPTION_RE.sub("", value)
        if value and value not in paths:
            paths.append(value)
    return paths


def _token_occurs(message: str, token: str) -> bool:
    if not token:
        return False
    if re.fullmatch(r"[a-z0-9.]+", token, re.I):
        return re.search(
            rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
            message,
            re.I,
        ) is not None
    return token.casefold() in message.casefold()


def _file_extension(filename: str) -> str:
    lower = filename.casefold()
    for extension in _KNOWN_FILE_EXTENSIONS:
        if lower.endswith(f".{extension}"):
            return extension
    return lower.rsplit(".", 1)[1] if "." in lower else ""


def _mentioned_file_extensions(message: str) -> set[str]:
    return {
        extension
        for extension in _KNOWN_FILE_EXTENSIONS
        if _token_occurs(message, extension)
    }


def _filename_reference_tokens(filename: str) -> list[str]:
    extension = _file_extension(filename)
    stem = filename[: -(len(extension) + 1)] if extension else filename
    return [
        token
        for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", stem.casefold())
        if len(token) >= 2
    ]


def _partial_file_matches(files: list[dict], message: str) -> list[dict]:
    """Return only the uniquely best filename/type references, never a guess."""
    mentioned_extensions = _mentioned_file_extensions(message)
    ranked: list[tuple[int, dict]] = []
    for item in files:
        filename = str(item.get("original_name") or "")
        extension = _file_extension(filename)
        if mentioned_extensions and extension not in mentioned_extensions:
            continue
        matched_tokens = [
            token
            for token in _filename_reference_tokens(filename)
            if _token_occurs(message, token)
        ]
        extension_match = bool(
            mentioned_extensions and extension in mentioned_extensions
        )
        if not matched_tokens and not extension_match:
            continue
        score = (100 if extension_match else 0) + sum(
            max(2, len(token)) for token in matched_tokens
        )
        ranked.append((score, item))
    if not ranked:
        return []
    best_score = max(score for score, _item in ranked)
    return [item for score, item in ranked if score == best_score]


def _file_choices(items: list[dict]) -> str:
    choices: list[str] = []
    for item in items[:5]:
        name = str(item.get("original_name") or "未命名文件")
        created_at = str(item.get("created_at") or "").replace("T", " ")
        choices.append(f"{name}（{created_at}）" if created_at else name)
    return "、".join(choices)


async def resolve_conversational_file_transfer(
    rt,
    session_id: str,
    message: str,
) -> TransferIntentResolution:
    """Resolve a natural-language upload without guessing any write target."""
    text = (message or "").strip()
    if _DEPLOYMENT_RE.search(text):
        return TransferIntentResolution(attempted=False)
    candidate = bool(_ACTION_RE.search(text) and _REMOTE_HINT_RE.search(text))
    if not candidate:
        return TransferIntentResolution(attempted=False)
    if not getattr(rt, "db", None) or not getattr(rt, "executor", None):
        return TransferIntentResolution(
            attempted=True,
            clarification="文件传输服务尚未初始化，请稍后重试。",
        )

    files = await list_session_files(rt.db, session_id)
    explicit_file_name = any(
        str(item.get("original_name") or "")
        and str(item["original_name"]).casefold() in text.casefold()
        for item in files
    )
    if not (_FILE_HINT_RE.search(text) or _RECENT_UPLOAD_RE.search(text) or explicit_file_name):
        return TransferIntentResolution(attempted=False)
    if not files:
        return TransferIntentResolution(
            attempted=True,
            clarification="当前会话没有可上传的文件，请先把文件添加到会话。",
        )

    exact_matches = [
        item
        for item in files
        if str(item.get("original_name") or "")
        and str(item["original_name"]).casefold() in text.casefold()
    ]
    if len(exact_matches) == 1:
        selected_file = exact_matches[0]
    elif len(exact_matches) > 1:
        return TransferIntentResolution(
            attempted=True,
            clarification=(
                f"匹配到多个同名会话文件（{_file_choices(exact_matches)}），"
                "请根据上传时间明确选择具体文件后再上传。"
            ),
        )
    elif _RECENT_UPLOAD_RE.search(text):
        # list_session_files is newest-first; "刚上传的" is an explicit and
        # deterministic reference to that first record, not an implicit guess.
        selected_file = files[0]
    else:
        partial_matches = _partial_file_matches(files, text)
        if len(partial_matches) == 1:
            selected_file = partial_matches[0]
        elif (
            len(files) == 1
            and not partial_matches
            and not _mentioned_file_extensions(text)
        ):
            selected_file = files[0]
        else:
            choices = partial_matches or files
            if len(files) == 1 and not partial_matches:
                return TransferIntentResolution(
                    attempted=True,
                    clarification=(
                        f"当前会话文件是 {_file_choices(files)}，"
                        "与描述的文件类型或名称不匹配，请确认文件名。"
                    ),
                )
            return TransferIntentResolution(
                attempted=True,
                clarification=(
                    f"匹配到多个可能的会话文件（{_file_choices(choices)}），"
                    "请明确要上传的完整文件名。"
                    if partial_matches
                    else (
                        f"当前会话有多个文件（{_file_choices(files)}），"
                        "请明确要上传的文件名。"
                    )
                ),
            )

    aliases = sorted(
        (
            alias
            for alias in getattr(rt.executor, "servers", {})
            if _alias_occurs(text, alias)
        ),
        key=len,
        reverse=True,
    )
    if len(aliases) != 1:
        return TransferIntentResolution(
            attempted=True,
            clarification=(
                "匹配到多个目标服务器，请只指定一个服务器别名。"
                if aliases
                else "请明确指定目标服务器别名。"
            ),
        )

    paths = _extract_absolute_paths(text)
    if len(paths) != 1:
        return TransferIntentResolution(
            attempted=True,
            clarification=(
                "匹配到多个远端路径，请只指定一个绝对目录。"
                if paths
                else "请明确指定远端绝对目录，例如 /tmp/shell-agent-uploads。"
            ),
        )

    return TransferIntentResolution(
        attempted=True,
        intent=ConversationalTransferIntent(
            file_id=str(selected_file["id"]),
            file_name=str(selected_file.get("original_name") or "file.bin"),
            target=aliases[0],
            remote_dir=paths[0],
            # Conversational uploads never overwrite an existing remote file.
            # Overwrite requires a separate, explicit structured flow.
            overwrite=False,
        ),
    )
