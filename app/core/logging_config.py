from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TextIO

from app.core.identifier import create_uuid_hex
from app.core.logging_contract import RuntimeLogContract, load_runtime_log_contract
from app.core.trace_middleware import get_current_request_id

APPLICATION_LOG_HANDLER_NAME = "boxteam-application"
_RUNTIME_INSTANCE_ID = create_uuid_hex(namespace="runtime-logging")


@dataclass(frozen=True, slots=True)
class RuntimeLogRecord:
    timestamp: str
    level: str
    service: str
    pid: int
    runtime_instance_id: str
    logger: str
    message: str
    context: Mapping[str, object] | None = None
    exception: str | None = None


def format_runtime_log_record(
    record: RuntimeLogRecord, *, pretty: bool, contract: RuntimeLogContract
) -> str:
    values = asdict(record)
    context = record.context or {}
    values["context"] = "".join(
        f" {key}={json.dumps(context[key], ensure_ascii=False, separators=(',', ':'))}"
        for key in contract.context_fields
        if context.get(key) is not None
    )
    values["level_upper"] = record.level.upper()
    template = contract.pretty_format if pretty else contract.compact_format
    rendered = template.format(**values)
    return f"{rendered}\n{record.exception}" if record.exception else rendered


class _StderrProxy:
    def write(self, content: str) -> int:
        return sys.stderr.write(content)

    def flush(self) -> None:
        sys.stderr.flush()


_STDERR_PROXY = _StderrProxy()


class _RuntimeFormatter(logging.Formatter):
    def __init__(self, *, service: str, pretty: bool, contract: RuntimeLogContract):
        super().__init__()
        self.service = service
        self.pretty = pretty
        self.contract = contract

    def format(self, record: logging.LogRecord) -> str:
        context = {
            key: getattr(record, key)
            for key in self.contract.context_fields
            if getattr(record, key, None) is not None
        }
        request_id = get_current_request_id()
        if request_id is not None:
            context.setdefault("request_id", request_id)
        exception = self.formatException(record.exc_info) if record.exc_info else None
        if record.stack_info:
            exception = f"{exception}\n{record.stack_info}" if exception else record.stack_info
        return format_runtime_log_record(
            RuntimeLogRecord(
                timestamp=datetime.fromtimestamp(record.created, UTC).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                level=record.levelname.lower(),
                service=self.service,
                pid=os.getpid(),
                runtime_instance_id=_RUNTIME_INSTANCE_ID,
                logger=record.name,
                message=record.getMessage(),
                context=context,
                exception=exception,
            ),
            pretty=self.pretty,
            contract=self.contract,
        )


class _RuntimeHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        # 标准 emit 会吞掉 sink 异常；此入口直接写入并保留原始失败。
        self.stream.write(self.format(record) + self.terminator)
        self.flush()


def configure_application_logging(
    *,
    service: str,
    level: str | int | None = None,
    pretty: bool | None = None,
    stream: TextIO | None = None,
) -> logging.Handler:
    """配置唯一的产品日志入口；进程托管方负责最终文件和轮转。"""
    contract = load_runtime_log_contract()
    selected_level = contract.default_level if level is None else level
    if isinstance(selected_level, str):
        resolved_level = contract.levels.get(selected_level.strip().lower())
    else:
        resolved_level = (
            selected_level
            if type(selected_level) is int and selected_level in contract.levels.values()
            else None
        )
    if resolved_level is None:
        raise ValueError(f"不支持的日志级别: {selected_level}")
    selected_pretty = contract.default_pretty if pretty is None else pretty
    if type(selected_pretty) is not bool:
        raise TypeError("日志 pretty 必须是布尔值")
    root_logger = logging.getLogger()
    handler = next(
        (candidate for candidate in root_logger.handlers
         if candidate.name == APPLICATION_LOG_HANDLER_NAME),
        None,
    )
    if handler is None:
        handler = _RuntimeHandler(stream if stream is not None else _STDERR_PROXY)
        handler.name = APPLICATION_LOG_HANDLER_NAME
        root_logger.addHandler(handler)
    elif not isinstance(handler, _RuntimeHandler):
        raise RuntimeError(f"日志 handler 名称冲突: {APPLICATION_LOG_HANDLER_NAME}")
    elif stream is not None:
        handler.setStream(stream)
    handler.setLevel(resolved_level)
    handler.setFormatter(_RuntimeFormatter(
        service=service, pretty=selected_pretty, contract=contract,
    ))
    root_logger.setLevel(resolved_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.setLevel(logging.NOTSET)
        uvicorn_logger.propagate = True
    return handler
