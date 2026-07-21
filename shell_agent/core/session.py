"""会话调度器（MVP 版）

负责路由：直接命令 vs 自然语言
"""
from __future__ import annotations

import aiosqlite
from loguru import logger

from shell_agent.core.models import AgentRequest, ConfirmMode, InputType
from shell_agent.executors.ssh import SSHExecutor
from shell_agent.llm.adapter import LLMAdapter
from shell_agent.safety.workflow import execute_with_confirmation
from shell_agent.utils.output import print_error, print_info


async def handle_request(
    db: aiosqlite.Connection,
    executor: SSHExecutor,
    llm: LLMAdapter,
    request: AgentRequest,
    auto_confirm: bool = False,
    confirm_mode: ConfirmMode | str = ConfirmMode.INTERACTIVE,
) -> None:
    """处理一个 Agent 请求"""
    input_type = request.detect_input_type()
    logger.info(
        f"处理请求: type={input_type.value} source={request.source} input={request.input!r}"
    )

    if input_type == InputType.COMMAND:
        await _handle_command(db, executor, request, auto_confirm, confirm_mode)
    else:
        await _handle_natural(db, executor, llm, request, auto_confirm, confirm_mode)


async def _handle_command(
    db: aiosqlite.Connection,
    executor: SSHExecutor,
    request: AgentRequest,
    auto_confirm: bool,
    confirm_mode: ConfirmMode | str,
) -> None:
    """直接命令路径：解析 → 确认 → 执行"""
    try:
        command = executor.normalize(request.input)
    except ValueError as e:
        print_error(f"命令解析失败: {e}")
        return
    await execute_with_confirmation(
        db=db,
        executor=executor,
        command=command,
        session_id=request.session_id,
        caller=request.caller,
        source=request.source,
        auto_confirm=auto_confirm,
        confirm_mode=confirm_mode,
    )


async def _handle_natural(
    db: aiosqlite.Connection,
    executor: SSHExecutor,
    llm: LLMAdapter,
    request: AgentRequest,
    auto_confirm: bool,
    confirm_mode: ConfirmMode | str,
) -> None:
    """自然语言路径：LLM 转换 → 确认 → 执行"""
    print_info("正在生成命令...")
    result = await llm.generate_command(request.input)

    if isinstance(result, str):
        # 纯文本回复（LLM 需要更多信息或仅回答问题）
        print_info(result)
        return

    command_str = result.get("command", "")
    intent = result.get("intent", "")
    if intent:
        print_info(f"意图: {intent}")

    if not command_str:
        print_error("LLM 未生成命令")
        return

    try:
        command = executor.normalize(command_str)
        command.source = "llm"
    except ValueError as e:
        print_error(f"LLM 生成的命令无法解析: {e}")
        logger.warning(f"LLM 生成命令无法解析: {command_str!r}")
        return

    await execute_with_confirmation(
        db=db,
        executor=executor,
        command=command,
        session_id=request.session_id,
        caller=request.caller,
        source=request.source,
        auto_confirm=auto_confirm,
        confirm_mode=confirm_mode,
    )
