"""终端输出：Rich 格式化"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()


def print_command_preview(
    target: str,
    command: str,
    source: str = "direct",
    risk_level: str | None = None,
    risk_reasons: list[str] | None = None,
) -> None:
    """打印命令预览"""
    body = f"[bold]目标[/bold]: {target}\n[bold]来源[/bold]: {source}"
    if risk_level:
        body += f"\n[bold]风险[/bold]: {risk_level}"
    if risk_reasons:
        body += "\n[bold]原因[/bold]: " + "；".join(risk_reasons)
    body += "\n\n[bold]命令[/bold]:"
    console.print(Panel.fit(body, border_style="cyan", title="命令预览"))
    console.print(Syntax(command, "bash", theme="monokai", word_wrap=True))


def print_execution_result(
    target: str,
    command: str,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    duration_ms: int,
    truncated: bool = False,
    timed_out: bool = False,
) -> None:
    """打印执行结果"""
    status = "✓ 成功" if exit_code == 0 else "✗ 失败"
    if timed_out:
        status = "⏱ 超时"
    color = "green" if exit_code == 0 else "red"

    header = (
        f"[bold {color}]{status}[/bold {color}]  "
        f"目标: {target}  耗时: {duration_ms}ms"
    )
    if exit_code is not None:
        header += f"  退出码: {exit_code}"
    console.print(header)

    if stdout:
        title = "stdout" + (" (已截断)" if truncated else "")
        console.print(Panel(stdout, title=title, border_style="green"))
    if stderr:
        title = "stderr" + (" (已截断)" if truncated else "")
        console.print(Panel(stderr, title=title, border_style="red"))


def print_info(msg: str) -> None:
    console.print(f"[cyan]ℹ[/cyan] {msg}")


def print_warn(msg: str) -> None:
    console.print(f"[yellow]⚠[/yellow] {msg}")


def print_error(msg: str) -> None:
    console.print(f"[red]✗[/red] {msg}")


def print_success(msg: str) -> None:
    console.print(f"[green]✓[/green] {msg}")
