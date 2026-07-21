"""确认工作流（MVP 版：所有命令都走 interactive 确认）

阶段 2 会加入分类器、auto_safe、dry_run 三档
"""
from __future__ import annotations

import aiosqlite
from loguru import logger

from shell_agent.core.models import (
    AuditRecord,
    ConfirmMode,
    ExecutionResult,
    PendingCommand,
)
from shell_agent.executors.ssh import SSHExecutor
from shell_agent.safety.audit import write_audit
from shell_agent.safety.classifier import classify_command
from shell_agent.utils.output import (
    print_command_preview,
    print_execution_result,
    print_info,
    print_warn,
)


async def confirm_interactive() -> bool:
    """交互式确认：用户回复 y/n"""
    print_info("确认执行? [y/n]: ", )
    # 临时简化：用 input()
    answer = input().strip().lower()
    return answer in ("y", "yes")


async def execute_with_confirmation(
    db: aiosqlite.Connection,
    executor: SSHExecutor,
    command: PendingCommand,
    session_id: str,
    caller: str,
    source: str,
    auto_confirm: bool = False,
    confirm_mode: ConfirmMode | str = ConfirmMode.INTERACTIVE,
) -> ExecutionResult | None:
    """完整的命令执行流程：预览 → 确认 → 执行 → 审计

    MVP 版本：所有命令都走 interactive 确认（auto_confirm=True 时跳过确认）
    """
    if isinstance(confirm_mode, str):
        confirm_mode = ConfirmMode(confirm_mode)

    # 1. 预览
    source_label = command.source
    if command.skill_name:
        source_label = f"Skill [{command.skill_name}]"
        if command.step_name:
            source_label += f" 步骤 {command.step_name}"
    risk = classify_command(command.actual_command)
    print_command_preview(
        command.target,
        command.actual_command,
        source_label,
        risk_level=risk.level.value,
        risk_reasons=risk.reasons,
    )

    # 2. 执行策略
    if confirm_mode == ConfirmMode.DRY_RUN:
        await _write_not_executed_audit(
            db=db,
            command=command,
            session_id=session_id,
            caller=caller,
            source=source,
            user_confirmed=None,
        )
        print_info("dry-run 模式：仅生成命令，不执行")
        return None

    if auto_confirm and risk.level.value == "critical":
        await _write_not_executed_audit(
            db=db,
            command=command,
            session_id=session_id,
            caller=caller,
            source=source,
            user_confirmed=None,
        )
        print_warn("critical 风险命令不能通过 --yes 自动执行，请手动确认后再运行")
        return None

    if confirm_mode == ConfirmMode.AUTO_SAFE and risk.level.value == "safe":
        print_info("auto_safe 模式：safe 命令自动执行")
        user_confirmed = True
    elif auto_confirm:
        user_confirmed = True
    else:
        user_confirmed = await confirm_interactive()

    if not user_confirmed:
        logger.info(f"用户取消执行: {command.actual_command}")
        await _write_not_executed_audit(
            db=db,
            command=command,
            session_id=session_id,
            caller=caller,
            source=source,
            user_confirmed=False,
        )
        print_info("已取消")
        return None

    # 3. 执行
    print_info("执行中...")
    try:
        result = await executor.execute(command)
    except Exception as e:
        logger.exception("执行器抛出异常")
        from shell_agent.core.models import ExecutionResult
        result = ExecutionResult(
            exit_code=None,
            stdout="",
            stderr=f"执行器异常: {e}",
            duration_ms=0,
        )

    # 4. 输出结果
    print_execution_result(
        target=command.target,
        command=command.actual_command,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
        truncated=result.truncated,
        timed_out=result.timed_out,
    )

    # 5. 审计落盘
    record = AuditRecord(
        command=command.actual_command,
        target=command.target,
        target_env=command.target_env,
        executor=command.executor,
        executed=True,
        source=source,
        caller=caller,
        session_id=session_id,
        user_confirmed=True,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        truncated=result.truncated,
        timed_out=result.timed_out,
    )
    await write_audit(db, record)
    return result


async def _write_not_executed_audit(
    db: aiosqlite.Connection,
    command: PendingCommand,
    session_id: str,
    caller: str,
    source: str,
    user_confirmed: bool | None,
) -> None:
    record = AuditRecord(
        command=command.actual_command,
        target=command.target,
        target_env=command.target_env,
        executor=command.executor,
        executed=False,
        source=source,
        caller=caller,
        session_id=session_id,
        user_confirmed=user_confirmed,
    )
    await write_audit(db, record)
