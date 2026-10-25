from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from app.core.logging_config import (
    APPLICATION_LOG_HANDLER_NAME,
    configure_application_logging,
)


@pytest.fixture
def isolated_root_logger() -> Iterator[logging.Logger]:
    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_application_handlers = [
        handler
        for handler in root_logger.handlers
        if handler.name == APPLICATION_LOG_HANDLER_NAME
    ]
    for handler in original_application_handlers:
        root_logger.removeHandler(handler)
    try:
        yield root_logger
    finally:
        current_application_handlers = [
            handler
            for handler in root_logger.handlers
            if handler.name == APPLICATION_LOG_HANDLER_NAME
        ]
        for handler in current_application_handlers:
            root_logger.removeHandler(handler)
            handler.close()
        for handler in original_application_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)


def test_pretty_application_log_is_written_to_stream(
    isolated_root_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    handler = configure_application_logging(
        level="info",
        pretty=True,
        stream=stream,
    )

    logging.getLogger("app.test").info("配置热重载成功: revision=test")

    assert handler.name == APPLICATION_LOG_HANDLER_NAME
    assert "INFO app.test 配置热重载成功: revision=test" in stream.getvalue()


def test_compact_application_log_uses_plain_text(
    isolated_root_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    configure_application_logging(
        level=logging.WARNING,
        pretty=False,
        stream=stream,
    )

    logging.getLogger("app.gateway.test").warning("Gateway 运行异常")

    assert stream.getvalue() == "WARNING app.gateway.test Gateway 运行异常\n"


def test_application_logging_configuration_is_idempotent(
    isolated_root_logger: logging.Logger,
) -> None:
    stream = io.StringIO()

    first = configure_application_logging(stream=stream)
    second = configure_application_logging(level="debug", stream=stream)

    assert first is second
    assert [
        handler
        for handler in isolated_root_logger.handlers
        if handler.name == APPLICATION_LOG_HANDLER_NAME
    ] == [first]
    assert isolated_root_logger.level == logging.DEBUG


def test_application_logging_rejects_unknown_level(
    isolated_root_logger: logging.Logger,
) -> None:
    with pytest.raises(ValueError, match="不支持的日志级别"):
        configure_application_logging(level="verbose")
