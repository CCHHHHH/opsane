"""Opsane CLI 入口

命令:
    opsane exec "<command>"      直接执行命令
    opsane run "<natural_lang>"  自然语言执行
    opsane shell                 进入 REPL
    opsane audit query           查询审计日志
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel

from shell_agent import __version__
from shell_agent.core.models import AgentRequest, ConfirmMode
from shell_agent.core.session import handle_request
from shell_agent.executors.ssh import SSHExecutor
from shell_agent.llm.adapter import LLMAdapter
from shell_agent.safety.audit import query_audit
from shell_agent.storage.database import connect, init_db
from shell_agent.utils.config import load_config, load_credentials, load_inventory
from shell_agent.utils.logging import setup_logging
from shell_agent.utils.output import print_error, print_info, print_warn

console = Console()


def _format_inventory_for_llm(servers: dict) -> str:
    """把服务器清单格式化成 LLM 上下文"""
    if not servers:
        return "(未配置任何服务器)"
    lines = []
    for alias, s in servers.items():
        lines.append(
            f"- {alias} (env={s.env}, role={s.role or 'N/A'}, tags={s.tags})"
        )
    return "\n".join(lines)


def _load_dotenv() -> None:
    """加载 .env 文件"""
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _prepare_environment(config_path: str) -> tuple:
    """同步部分：加载配置、初始化日志

    返回 (config, credentials, servers)
    """
    _load_dotenv()
    config = load_config(config_path)
    setup_logging(config.logging.level, config.logging.dir)
    credentials = load_credentials()
    servers = load_inventory()

    if not config.llm.api_key:
        print_warn("OPENAI_API_KEY 未设置，自然语言功能将不可用")

    if not servers:
        print_warn("未配置任何服务器，请复制 config/inventory.yaml.example 为 inventory.yaml 并填写")

    return config, credentials, servers


def _resolve_confirm_mode(
    auto_confirm: bool,
    auto_safe: bool,
    dry_run: bool,
) -> ConfirmMode:
    selected = [auto_confirm, auto_safe, dry_run]
    if sum(1 for item in selected if item) > 1:
        raise click.ClickException("--yes、--auto-safe、--dry-run 只能选择一个")
    if dry_run:
        print_info("dry-run 模式：只生成和展示命令，不执行")
        return ConfirmMode.DRY_RUN
    if auto_safe:
        print_info("auto_safe 模式：仅 safe 风险命令自动执行")
        return ConfirmMode.AUTO_SAFE
    if auto_confirm:
        print_warn("⚠ 跳过确认模式：critical 风险命令仍会被阻止自动执行")
    return ConfirmMode.INTERACTIVE


async def _build_runtime(config, credentials, servers):
    """异步部分：初始化 DB、执行器、LLM"""
    await init_db(config.storage.sqlite_path)
    db = await connect(config.storage.sqlite_path)
    executor = SSHExecutor(
        servers=servers,
        credentials=credentials,
        max_per_host=config.ssh.max_per_host,
        idle_timeout=config.ssh.idle_timeout,
        total_max=config.ssh.total_max,
        default_timeout=config.ssh.default_timeout,
    )
    llm = LLMAdapter(
        config=config.llm,
        instances_description=_format_inventory_for_llm(servers),
    )
    return db, executor, llm


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Opsane - 自然语言驱动的智能运维工作台"""


@main.command()
@click.argument("command")
@click.option("--config", "config_path", default="config/agent.yaml", help="配置文件路径")
@click.option("--yes", "-y", "auto_confirm", is_flag=True, help="跳过确认（危险！）")
@click.option("--auto-safe", is_flag=True, help="仅自动执行 safe 风险命令")
@click.option("--dry-run", is_flag=True, help="只预览命令，不执行")
def exec(
    command: str,
    config_path: str,
    auto_confirm: bool,
    auto_safe: bool,
    dry_run: bool,
) -> None:
    """直接执行命令"""
    confirm_mode = _resolve_confirm_mode(auto_confirm, auto_safe, dry_run)

    config, credentials, servers = _prepare_environment(config_path)

    async def run():
        db, executor, llm = await _build_runtime(config, credentials, servers)
        try:
            request = AgentRequest(
                input=command,
                input_type="command",
                source="cli",
                caller=os.getenv("USER", "anonymous"),
            )
            await handle_request(
                db,
                executor,
                llm,
                request,
                auto_confirm=auto_confirm,
                confirm_mode=confirm_mode,
            )
        finally:
            await executor.close_all()
            await db.close()

    asyncio.run(run())


@main.command()
@click.argument("input_text")
@click.option("--config", "config_path", default="config/agent.yaml", help="配置文件路径")
@click.option("--yes", "-y", "auto_confirm", is_flag=True, help="跳过确认（危险！）")
@click.option("--auto-safe", is_flag=True, help="仅自动执行 safe 风险命令")
@click.option("--dry-run", is_flag=True, help="只生成命令，不执行")
def run(
    input_text: str,
    config_path: str,
    auto_confirm: bool,
    auto_safe: bool,
    dry_run: bool,
) -> None:
    """自然语言执行"""
    confirm_mode = _resolve_confirm_mode(auto_confirm, auto_safe, dry_run)

    config, credentials, servers = _prepare_environment(config_path)

    async def run_async():
        db, executor, llm = await _build_runtime(config, credentials, servers)
        try:
            request = AgentRequest(
                input=input_text,
                input_type="auto",
                source="cli",
                caller=os.getenv("USER", "anonymous"),
            )
            await handle_request(
                db,
                executor,
                llm,
                request,
                auto_confirm=auto_confirm,
                confirm_mode=confirm_mode,
            )
        finally:
            await executor.close_all()
            await db.close()

    asyncio.run(run_async())


@main.command()
@click.option("--config", "config_path", default="config/agent.yaml", help="配置文件路径")
def shell(config_path: str) -> None:
    """进入交互式 REPL"""
    config, credentials, servers = _prepare_environment(config_path)

    console.print(
        Panel.fit(
            f"[bold]Opsane v{__version__}[/bold]\n"
            f"输入命令或自然语言，Ctrl+D 退出",
            border_style="cyan",
        )
    )

    async def repl():
        db, executor, llm = await _build_runtime(config, credentials, servers)
        try:
            while True:
                try:
                    user_input = input("\n>>> ").strip()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n再见")
                    break
                if not user_input:
                    continue
                if user_input in ("exit", "quit"):
                    break
                request = AgentRequest(
                    input=user_input,
                    input_type="auto",
                    source="cli",
                    caller=os.getenv("USER", "anonymous"),
                )
                try:
                    await handle_request(db, executor, llm, request)
                except Exception as e:
                    print_error(f"执行失败: {e}")
        finally:
            await executor.close_all()
            await db.close()

    asyncio.run(repl())


@main.group()
def audit() -> None:
    """审计日志"""


@audit.command("query")
@click.option("--target", help="按目标过滤")
@click.option("--limit", default=20, help="返回记录数")
@click.option("--config", "config_path", default="config/agent.yaml", help="配置文件路径")
def audit_query(target: Optional[str], limit: int, config_path: str) -> None:
    """查询审计日志"""
    config, _, _ = _prepare_environment(config_path)

    async def run_query():
        await init_db(config.storage.sqlite_path)
        db = await connect(config.storage.sqlite_path)
        try:
            records = await query_audit(db, target=target, limit=limit)
            if not records:
                print_info("无审计记录")
                return
            from rich.table import Table
            table = Table(title="审计日志")
            table.add_column("时间", style="cyan")
            table.add_column("目标", style="yellow")
            table.add_column("命令", style="white")
            table.add_column("执行", style="green")
            table.add_column("退出码", style="magenta")
            for r in records:
                cmd = r["command"]
                table.add_row(
                    r["timestamp"],
                    r["target"] or "-",
                    cmd[:60] + "..." if len(cmd) > 60 else cmd,
                    "✓" if r["executed"] else "✗",
                    str(r["exit_code"]) if r["exit_code"] is not None else "-",
                )
            console.print(table)
        finally:
            await db.close()

    asyncio.run(run_query())


@main.command()
@click.option("--config", "config_path", default="config/agent.yaml", help="配置文件路径")
@click.option("--host", default="127.0.0.1", help="监听地址")
@click.option("--port", default=8000, type=int, help="监听端口")
def serve(config_path: str, host: str, port: int) -> None:
    """启动 Web 服务（含可视化界面 + API）"""
    import uvicorn
    from shell_agent.web.app import create_app

    # 加载 .env
    _load_dotenv()

    config = load_config(config_path)
    setup_logging(config.logging.level, config.logging.dir)

    web_app = create_app(config_path=config_path)
    # Keep service bootstrap output ASCII-only. PyInstaller's redirected Windows
    # streams may use a legacy code page even when the parent requests UTF-8.
    click.echo(f"Opsane Web service starting: http://{host}:{port}")
    click.echo("Open the address above in a browser to use Opsane.")
    uvicorn.run(web_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
