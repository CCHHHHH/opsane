"""日志：loguru 配置"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(level: str = "INFO", log_dir: str = "data/logs") -> None:
    """配置 loguru：控制台 + 文件"""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan> - {message}",
    )
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.add(
        Path(log_dir) / "shell-agent-{time:YYYY-MM-DD}.log",
        level=level,
        rotation="00:00",
        retention="14 days",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {module}:{function}:{line} - {message}",
    )
