"""隔离 SDK 日志测试对进程根 logger 的修改。"""

import logging
from collections.abc import Iterator

import pytest
from boxteam_plugin_sdk.logging import APPLICATION_LOG_HANDLER_NAME


@pytest.fixture
def isolated_root_logger() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    loggers = [root, *(logging.getLogger(name) for name in (
        "uvicorn", "uvicorn.error", "uvicorn.access",
    ))]
    originals = [(logger, logger.handlers[:], logger.level, logger.propagate)
                 for logger in loggers]
    root.handlers = [handler for handler in root.handlers
                     if handler.name != APPLICATION_LOG_HANDLER_NAME]
    try:
        yield root
    finally:
        for handler in root.handlers:
            if handler.name == APPLICATION_LOG_HANDLER_NAME:
                handler.close()
        for logger, handlers, level, propagate in originals:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
