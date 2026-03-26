"""
Business Logic:
    提供统一的日志配置，所有模块共用同一个logger，
    便于调试和问题排查。

Code Logic:
    基于Python logging + Rich handler，支持控制台彩色输出。
"""

import logging

from rich.logging import RichHandler


def setup_logger(name: str = "deepseek_ocr", level: int = logging.INFO) -> logging.Logger:
    """
    Business Logic:
        创建并配置项目logger，确保所有模块的日志格式统一。

    Code Logic:
        使用RichHandler输出到控制台，设置格式为时间+模块+消息。
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = RichHandler(rich_tracebacks=True, show_path=False)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(level)
    return logger


logger = setup_logger()
