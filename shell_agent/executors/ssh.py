"""SSH 执行器：在远程服务器执行 shell 命令"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from pathlib import Path
import posixpath
import re
import shlex
import time
from typing import Any

import asyncssh
from loguru import logger

from shell_agent.core.models import ExecutionResult, PendingCommand
from shell_agent.utils.config import Credential, ServerEntry


_REMOTE_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class VerifiedUploadResult:
    remote_path: str
    size: int
    sha256: str


def normalize_upload_destination(remote_dir: str, remote_name: str) -> tuple[str, str, str]:
    """Validate and normalize one POSIX upload destination."""
    supplied_dir = remote_dir or ""
    supplied_name = remote_name or ""
    if _REMOTE_CONTROL_CHARS.search(supplied_dir) or _REMOTE_CONTROL_CHARS.search(supplied_name):
        raise ValueError("远端路径不能包含控制字符")
    raw_dir = supplied_dir.strip()
    raw_name = supplied_name.strip()
    if not raw_dir.startswith("/"):
        raise ValueError("远端目录必须是绝对路径")
    if any(part in {".", ".."} for part in raw_dir.split("/")):
        raise ValueError("远端目录不能包含 . 或 .. 路径段")
    normalized_dir = posixpath.normpath(raw_dir)
    if not normalized_dir.startswith("/"):
        raise ValueError("远端目录必须是绝对路径")
    if not raw_name or raw_name in {".", ".."}:
        raise ValueError("远端文件名无效")
    if posixpath.basename(raw_name) != raw_name or "/" in raw_name or "\\" in raw_name:
        raise ValueError("远端文件名不能包含路径")
    if len(raw_name.encode("utf-8")) > 200:
        raise ValueError("远端文件名过长")
    return normalized_dir, raw_name, posixpath.join(normalized_dir, raw_name)


def _local_file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


async def _remote_file_digest(conn, sftp, path: str) -> tuple[int, str]:
    attrs = await sftp.stat(path)
    remote_size = int(attrs.size or 0)
    try:
        result = await conn.run(
            f"sha256sum -- {shlex.quote(path)}",
            check=False,
        )
        match = re.match(r"^([0-9a-fA-F]{64})(?:\s|$)", str(result.stdout or ""))
        if result.exit_status == 0 and match:
            return remote_size, match.group(1).lower()
    except (OSError, asyncssh.Error):
        pass

    # Portable fallback for SFTP servers without a shell/sha256sum. It costs a
    # second transfer but preserves cryptographic verification semantics.
    digest = hashlib.sha256()
    size = 0
    async with sftp.open(path, "rb") as stream:
        while chunk := await stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def truncate_output(text: str, limit: int = 10000) -> tuple[str, bool]:
    """截断长输出，保留头尾"""
    if len(text) <= limit:
        return text, False
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    skipped = len(text) - limit
    return f"{head}\n\n... [{skipped} chars truncated] ...\n\n{tail}", True


_COMMON_SHELL_ALIASES = {
    "ll": "ls -l",
    "la": "ls -la",
    "l": "ls -CF",
}
_COMMON_ALIAS_PATTERN = re.compile(
    r"^(?P<prefix>\s*)(?P<alias>ll|la|l)(?P<suffix>(?:\s|$|[;&|<>]).*)$",
    re.DOTALL,
)


def parse_ssh_command(raw: str) -> tuple[str, str] | None:
    """从 'ssh alias "command"' 解析出 (target, command)"""
    try:
        parts = shlex.split(raw, posix=True)
    except ValueError:
        return None
    if len(parts) >= 3 and parts[0] == "ssh":
        return parts[1], " ".join(parts[2:])
    return None


def expand_common_shell_aliases(command: str) -> str:
    """展开常见交互式 shell alias，让远程一次性命令更接近登录 shell。"""
    m = _COMMON_ALIAS_PATTERN.match(command)
    if not m:
        return command
    alias = m.group("alias")
    return f"{m.group('prefix')}{_COMMON_SHELL_ALIASES[alias]}{m.group('suffix')}"


class SSHExecutor:
    """SSH 执行器，支持连接池复用"""

    def __init__(
        self,
        servers: dict[str, ServerEntry],
        credentials: dict[str, Credential],
        max_per_host: int = 3,
        idle_timeout: int = 300,
        total_max: int = 50,
        default_timeout: int = 60,
        trust_unknown_hosts: bool = False,
    ) -> None:
        self.servers = servers
        self.credentials = credentials
        self.max_per_host = max_per_host
        self.idle_timeout = idle_timeout
        self.total_max = total_max
        self.default_timeout = default_timeout
        self.trust_unknown_hosts = trust_unknown_hosts
        # 连接池：target -> list[asyncssh.SSHClientConnection]
        self._pool: dict[str, list[asyncssh.SSHClientConnection]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def resolve_server(self, alias: str) -> ServerEntry:
        canonical = self.resolve_server_alias(alias)
        if canonical is None:
            raise ValueError(f"未知服务器别名: {alias}")
        return self.servers[canonical]

    def resolve_server_alias(self, alias: str) -> str | None:
        """返回配置中的真实服务器别名，允许 LLM 大小写不一致。"""
        if alias in self.servers:
            return alias
        alias_lower = alias.lower()
        for configured_alias in self.servers:
            if configured_alias.lower() == alias_lower:
                return configured_alias
        return None

    def resolve_credential(self, cred_id: str) -> Credential:
        if cred_id not in self.credentials:
            raise ValueError(f"未知凭证 ID: {cred_id}")
        return self.credentials[cred_id]

    def normalize(self, raw_command: str) -> PendingCommand:
        """从原始命令解析出 PendingCommand"""
        parsed = parse_ssh_command(raw_command)
        if parsed is None:
            raise ValueError(f"无法解析 SSH 命令: {raw_command}")
        target_alias, actual_command = parsed
        canonical_alias = self.resolve_server_alias(target_alias)
        if canonical_alias is None:
            raise ValueError(f"未知服务器别名: {target_alias}")
        server = self.resolve_server(canonical_alias)
        actual_command = expand_common_shell_aliases(actual_command)
        return PendingCommand(
            raw=raw_command,
            target=canonical_alias,
            target_env=server.env,
            executor="ssh",
            actual_command=actual_command,
        )

    async def _get_connection(self, target: str) -> asyncssh.SSHClientConnection:
        """从池中获取连接，必要时新建"""
        if target not in self._pool:
            self._pool[target] = []
            self._locks[target] = asyncio.Lock()

        async with self._locks[target]:
            # 池里有可用连接
            if self._pool[target]:
                conn = self._pool[target].pop()
                logger.debug(f"复用 SSH 连接: {target}")
                return conn

            # 新建连接
            server = self.resolve_server(target)
            cred = self.resolve_credential(server.ssh_credential)
            kwargs: dict[str, Any] = {"username": cred.username}
            if self.trust_unknown_hosts:
                kwargs["known_hosts"] = None
            if cred.type == "password" and cred.password:
                kwargs["password"] = cred.password
            elif cred.type == "key" and cred.private_key:
                kwargs["client_keys"] = [cred.private_key]
                if cred.passphrase:
                    kwargs["passphrase"] = cred.passphrase

            logger.info(f"建立 SSH 连接: {target} ({server.host}:{server.port})")
            conn = await asyncssh.connect(
                server.host, port=server.port, **kwargs
            )
            return conn

    async def _release_connection(
        self, target: str, conn: asyncssh.SSHClientConnection
    ) -> None:
        """归还连接到池"""
        if (
            target in self._pool
            and len(self._pool[target]) < self.max_per_host
            and not conn.is_closed()
        ):
            self._pool[target].append(conn)
            logger.debug(f"归还 SSH 连接: {target} (池中 {len(self._pool[target])})")
        else:
            conn.close()
            logger.debug(f"关闭 SSH 连接: {target}")

    async def execute(
        self, command: PendingCommand, timeout: int | None = None
    ) -> ExecutionResult:
        """在目标服务器执行命令"""
        timeout = timeout or self.default_timeout
        conn = await self._get_connection(command.target)
        start = time.monotonic()
        timed_out = False
        try:
            # 用 bash -lc 包裹以支持管道/环境变量，shlex.quote 防注入
            wrapped = f"bash -lc {shlex.quote(command.actual_command)}"
            logger.info(
                f"执行 SSH 命令: target={command.target} cmd={command.actual_command!r}"
            )
            result = await asyncio.wait_for(
                conn.run(wrapped, check=False), timeout=timeout
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            stdout, stdout_truncated = truncate_output(result.stdout or "")
            stderr, stderr_truncated = truncate_output(result.stderr or "")
            return ExecutionResult(
                exit_code=result.exit_status,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                truncated=stdout_truncated or stderr_truncated,
                timed_out=False,
            )
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning(f"SSH 命令超时: target={command.target} timeout={timeout}s")
            return ExecutionResult(
                exit_code=None,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                duration_ms=duration_ms,
                timed_out=True,
            )
        finally:
            await self._release_connection(command.target, conn)

    async def upload_file(
        self,
        target: str,
        local_path: str | Path,
        remote_dir: str,
        remote_name: str,
        timeout: int | None = None,
    ) -> str:
        """Backward-compatible verified upload which atomically replaces a file."""
        result = await self.upload_file_verified(
            target=target,
            local_path=local_path,
            remote_dir=remote_dir,
            remote_name=remote_name,
            overwrite=True,
            timeout=timeout,
        )
        return result.remote_path

    async def upload_file_verified(
        self,
        *,
        target: str,
        local_path: str | Path,
        remote_dir: str,
        remote_name: str,
        overwrite: bool = False,
        expected_size: int | None = None,
        expected_sha256: str = "",
        operation_id: str = "",
        timeout: int | None = None,
    ) -> VerifiedUploadResult:
        """Upload to a part file, verify it, then atomically publish it."""
        timeout = timeout or self.default_timeout
        canonical_alias = self.resolve_server_alias(target)
        if canonical_alias is None:
            raise ValueError(f"未知服务器别名: {target}")
        remote_dir, remote_name, remote_path = normalize_upload_destination(
            remote_dir, remote_name
        )
        local = Path(local_path).resolve()
        if not local.exists() or not local.is_file():
            raise ValueError(f"本地文件不存在: {local}")
        local_size, local_sha256 = await asyncio.to_thread(_local_file_digest, local)
        if expected_size is not None and local_size != int(expected_size):
            raise ValueError("本地文件大小与上传记录不一致")
        if expected_sha256 and local_sha256.lower() != expected_sha256.lower():
            raise ValueError("本地文件 SHA-256 与上传记录不一致")

        safe_operation = re.sub(r"[^A-Za-z0-9_-]", "", operation_id)[:32]
        if not safe_operation:
            safe_operation = f"{time.monotonic_ns():x}"
        part_path = posixpath.join(
            remote_dir, f".{remote_name}.{safe_operation}.part"
        )
        conn = await self._get_connection(canonical_alias)
        try:
            logger.info(
                "上传会话文件: target={} remote={} size={} sha256={}",
                canonical_alias,
                remote_path,
                local_size,
                local_sha256,
            )
            async with conn.start_sftp_client() as sftp:
                try:
                    async with asyncio.timeout(timeout):
                        await sftp.makedirs(remote_dir, exist_ok=True)
                        await sftp.put(
                            str(local),
                            part_path,
                            preserve=False,
                            recurse=False,
                            follow_symlinks=False,
                        )
                        remote_size, remote_sha256 = await _remote_file_digest(
                            conn, sftp, part_path
                        )
                        if remote_size != local_size:
                            raise IOError(
                                f"远端文件大小校验失败: {remote_size} != {local_size}"
                            )
                        if remote_sha256.lower() != local_sha256.lower():
                            raise IOError("远端文件 SHA-256 校验失败")
                        if overwrite:
                            await sftp.posix_rename(part_path, remote_path)
                        else:
                            # Standard SFTP rename has no overwrite flag here and
                            # therefore publishes atomically or fails if final exists.
                            await sftp.rename(part_path, remote_path)
                except BaseException:
                    try:
                        await asyncio.shield(sftp.remove(part_path))
                    except (OSError, asyncssh.SFTPError):
                        pass
                    raise
            return VerifiedUploadResult(
                remote_path=remote_path,
                size=local_size,
                sha256=local_sha256,
            )
        finally:
            await self._release_connection(canonical_alias, conn)

    async def close_all(self) -> None:
        """关闭所有连接"""
        for target, conns in self._pool.items():
            for conn in conns:
                if not conn.is_closed():
                    conn.close()
            logger.info(f"关闭 {target} 的 {len(conns)} 个连接")
        self._pool.clear()
