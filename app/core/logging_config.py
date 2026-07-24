from __future__ import annotations

import logging
import sys
import time
from typing import TextIO

APPLICATION_LOG_HANDLER_NAME = "boxteam-application"


class _StderrProxy:
    def write(self, content: str) -> int:
        return sys.stderr.write(content)

    def flush(self) -> None:
        sys.stderr.flush()


_STDERR_PROXY = _StderrProxy()


class _UtcPrettyFormatter(logging.Formatter):
    converter = time.gmtime


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    normalized = level.strip().upper()
    resolved = logging.getLevelNamesMapping().get(normalized)
    if resolved is None:
        raise ValueError(f"不支持的日志级别: {level}")
    return resolved


def _formatter(*, pretty: bool) -> logging.Formatter:
    if not pretty:
        return logging.Formatter("%(levelname)s %(name)s %(message)s")
    return _UtcPrettyFormatter(
        fmt="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def configure_application_logging(
    *,
    level: str | int = logging.INFO,
    pretty: bool = True,
    stream: TextIO | None = None,
) -> logging.Handler:
    """配置应用 logger 到 stderr；进程托管方负责最终日志文件。"""
    resolved_level = _resolve_level(level)
    root_logger = logging.getLogger()
    handler = next(
        (
            candidate
            for candidate in root_logger.handlers
            if candidate.name == APPLICATION_LOG_HANDLER_NAME
        ),
        None,
    )
    target_stream = stream or _STDERR_PROXY
    if handler is None:
        handler = logging.StreamHandler(target_stream)
        handler.name = APPLICATION_LOG_HANDLER_NAME
        root_logger.addHandler(handler)
    elif isinstance(handler, logging.StreamHandler) and stream is not None:
        handler.setStream(target_stream)
    elif not isinstance(handler, logging.StreamHandler):
        raise RuntimeError(
            f"日志 handler 名称冲突: {APPLICATION_LOG_HANDLER_NAME}"
        )
    handler.setLevel(resolved_level)
    handler.setFormatter(_formatter(pretty=pretty))
    root_logger.setLevel(resolved_level)
    return handler
