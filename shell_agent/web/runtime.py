"""Web 后端运行时：管理全局唯一的 Agent 实例"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import aiosqlite
from fastapi import FastAPI
from loguru import logger

from shell_agent.core.context import SessionContext
from shell_agent.core.models import PendingCommand
from shell_agent.executors.ssh import SSHExecutor
from shell_agent.llm.adapter import LLMAdapter
from shell_agent.storage.database import connect, init_db
from shell_agent.storage.file_transfers import interrupt_running_file_transfers
from shell_agent.storage.tasks import reconcile_orphaned_tasks
from shell_agent.utils.config import (
    AppConfig, load_config, load_credentials, load_inventory, load_services,
    ServerEntry, LLMConfig,
)


class Runtime:
    """全局运行时：管理 config/executor/llm/db 单例"""

    _instance: Optional["Runtime"] = None

    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        # 加载 .env
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

        self.config = load_config(config_path)
        self.credentials = load_credentials()
        self.servers = load_inventory()
        self.services = load_services()
        self.db: aiosqlite.Connection | None = None
        self.executor: SSHExecutor | None = None
        self.llm: LLMAdapter | None = None
        # 待确认的命令（按 session_id 索引）
        self.pending_commands: dict[str, PendingCommand] = {}
        # 待确认的操作方案（按 session_id 索引）
        self.pending_operation_plans: dict[str, dict] = {}
        # WebSocket 会话短期上下文（按 session_id 索引）
        self.session_contexts: dict[str, SessionContext] = {}
        self.context_summary_locks: dict[str, asyncio.Lock] = {}
        # 正在执行的 WebSocket 命令任务（按 session/channel 索引）
        self.running_tasks: dict[str, asyncio.Task] = {}
        # 每个运行阶段当前持有的持久化任务 ID，用于会话恢复时校验任务是否真实存活。
        self.running_task_ids: dict[str, str] = {}
        # 任务完成后的知识学习不影响前台任务状态。
        self.background_tasks: set[asyncio.Task] = set()
        self.learning_task_ids: set[str] = set()
        # Durable deployment runbooks are initialized once per process.  The
        # route layer can call the same method defensively without creating a
        # second storage/runtime pair.
        self.deployment_runtime = None
        self._deployment_init_lock = asyncio.Lock()

    async def start(self) -> None:
        """初始化数据库、执行器、LLM"""
        await init_db(self.config.storage.sqlite_path)
        self.db = await connect(self.config.storage.sqlite_path)
        interrupted = await reconcile_orphaned_tasks(self.db)
        if interrupted:
            logger.warning(f"启动时已收口 {len(interrupted)} 个未完成的孤儿任务")
        interrupted_transfers = await interrupt_running_file_transfers(self.db)
        if interrupted_transfers:
            logger.warning(
                f"启动时已收口 {len(interrupted_transfers)} 个未完成的文件传输"
            )
        self.executor = SSHExecutor(
            servers=self.servers,
            credentials=self.credentials,
            max_per_host=self.config.ssh.max_per_host,
            idle_timeout=self.config.ssh.idle_timeout,
            total_max=self.config.ssh.total_max,
            default_timeout=self.config.ssh.default_timeout,
            trust_unknown_hosts=self.config.ssh.trust_unknown_hosts,
        )
        await self.initialize_deployment_runtime()
        instances_desc = self._format_inventory_for_llm(self.servers, self.services)
        self.llm = LLMAdapter(
            config=self.config.llm, instances_description=instances_desc
        )
        logger.info("Web Runtime 启动完成")

    async def initialize_deployment_runtime(self):
        """Initialize deployment persistence, reconciliation and SSH adapter."""
        if self.deployment_runtime is not None:
            return self.deployment_runtime
        async with self._deployment_init_lock:
            if self.deployment_runtime is not None:
                return self.deployment_runtime
            if self.db is None or self.executor is None:
                raise RuntimeError("部署服务依赖尚未初始化")
            from shell_agent.runbooks import (
                DeploymentRunbookRuntime,
                RunbookStorage,
            )
            from shell_agent.runbooks.ssh_executor import SSHDeploymentExecutor

            storage = RunbookStorage(self.db)
            await storage.initialize()
            interrupted = await storage.reconcile_interrupted_runs()
            if interrupted:
                logger.warning(
                    f"启动时已将 {len(interrupted)} 个未完成部署任务标记为 unknown"
                )
            self.deployment_runtime = DeploymentRunbookRuntime(
                storage, SSHDeploymentExecutor(self)
            )
            return self.deployment_runtime

    async def stop(self) -> None:
        for task in list(self.background_tasks):
            if not task.done():
                task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        if self.executor:
            await self.executor.close_all()
        if self.db:
            await self.db.close()

    def _format_inventory_for_llm(self, servers: dict, services: dict | None = None) -> str:
        if not servers and not services:
            return "(未配置任何服务器)"
        lines = []
        if servers:
            lines.append("服务器清单:")
            for alias, s in servers.items():
                lines.append(f"- {alias} (env={s.env}, role={s.role or 'N/A'})")
        if services:
            lines.append(
                "服务画像会根据当前问题按需注入；不要假设未出现在当前上下文中的服务位置。"
            )
        return "\n".join(lines)

    def secret_values(self) -> list[str]:
        values: list[str] = []
        for credential in self.credentials.values():
            for key in ("password", "private_key", "passphrase"):
                value = getattr(credential, key, None)
                if value:
                    values.append(str(value))
        if self.config.llm.api_key:
            values.append(self.config.llm.api_key)
        return values

    async def reload(self) -> None:
        """重新加载配置（不重连已建立的连接）"""
        self.config = load_config(self.config_path)
        self.credentials = load_credentials()
        self.servers = load_inventory()
        self.services = load_services()
        instances_desc = self._format_inventory_for_llm(self.servers, self.services)
        self.llm = LLMAdapter(
            config=self.config.llm, instances_description=instances_desc
        )
        # executor 的配置需更新；关闭旧连接，避免沿用旧 host-key 策略。
        if self.executor:
            await self.executor.close_all()
            self.executor.servers = self.servers
            self.executor.credentials = self.credentials
            self.executor.max_per_host = self.config.ssh.max_per_host
            self.executor.idle_timeout = self.config.ssh.idle_timeout
            self.executor.total_max = self.config.ssh.total_max
            self.executor.default_timeout = self.config.ssh.default_timeout
            self.executor.trust_unknown_hosts = self.config.ssh.trust_unknown_hosts


# 全局 runtime 实例
_runtime: Optional[Runtime] = None


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("Runtime 未初始化，请先调用 init_runtime()")
    return _runtime


def init_runtime(config_path: str) -> Runtime:
    global _runtime
    _runtime = Runtime(config_path)
    return _runtime
