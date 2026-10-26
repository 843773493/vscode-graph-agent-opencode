from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import TextIO

from app.core.identifier import create_uuid_hex
from app.core.logging_contract import RuntimeLogContract, load_runtime_log_contract
from app.core.trace_middleware import get_current_request_id

APPLICATION_LOG_HANDLER_NAME = "boxteam-application"
_RUNTIME_INSTANCE_ID = create_uuid_hex(namespace="runtime-logging")
_CONFIGURATION_LOCK = RLock()


class RuntimeLoggingConflictError(ValueError):
    """目标实例或当前 revision 已变化，调用方必须重新核实。"""


@dataclass(frozen=True, slots=True)
class RuntimeLoggingState:
    service: str
    pid: int
    runtime_instance_id: str
    applied_revision: str | None
    level: str
    pretty: bool
    log_sink: dict[str, str] | None


def _read_runtime_log_sink() -> dict[str, str] | None:
    serialized = os.environ.get("BOXTEAM_RUNTIME_LOG_SINK")
    if serialized is None:
        return None
    sink = json.loads(serialized)
    fields = {"path", "owner_id", "role", "source", "device", "inode"}
    if not isinstance(sink, dict) or set(sink) != fields or any(
        not isinstance(value, str) or not value for value in sink.values()
    ) or not Path(sink["path"]).is_absolute():
        raise ValueError("受管进程日志去向非法")
    return sink


def _verify_runtime_log_sink(sink: dict[str, str] | None) -> dict[str, str] | None:
    if sink is None:
        return None
    expected = (sink["device"], sink["inode"])
    # 不能只信环境里的路径：继承的两个实际输出描述符与当前文件都必须一致。
    identities = [os.fstat(1), os.fstat(2), Path(sink["path"]).stat()]
    if any((str(identity.st_dev), str(identity.st_ino)) != expected for identity in identities):
        raise RuntimeError("受管进程实际日志描述符与冻结去向不一致")
    return dict(sink)


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
    applied_revision: str | None = None
    log_sink: dict[str, str] | None = None

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
    revision: str | None = None,
) -> _RuntimeHandler:
    """配置唯一的产品日志入口；进程托管方负责最终文件和轮转。"""
    with _CONFIGURATION_LOCK:
        contract = load_runtime_log_contract()
        root_logger = logging.getLogger()
        handler = next(
            (candidate for candidate in root_logger.handlers
             if candidate.name == APPLICATION_LOG_HANDLER_NAME), None,
        )
        if level is None and pretty is None and stream is None and revision is None:
            if isinstance(handler, _RuntimeHandler) and isinstance(handler.formatter, _RuntimeFormatter) and handler.formatter.service == service:
                level, pretty, revision = handler.level, handler.formatter.pretty, handler.applied_revision
            elif (startup := os.environ.get("BOXTEAM_RUNTIME_LOGGING")) is not None:
                payload = json.loads(startup)
                if (not isinstance(payload, dict) or set(payload) != {"revision", "level", "pretty"}
                    or not isinstance(payload["level"], str)):
                    raise ValueError("受管进程启动日志设置非法")
                level, pretty, revision = payload["level"], payload["pretty"], payload["revision"]
                if not isinstance(revision, str) or not revision.strip() or type(pretty) is not bool:
                    raise ValueError("受管进程启动日志设置缺少有效 revision 或格式")
        if revision is not None and (not isinstance(revision, str) or not revision.strip()):
            raise ValueError("日志 revision 非法")
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
        if handler is None:
            handler = _RuntimeHandler(stream if stream is not None else _STDERR_PROXY)
            handler.log_sink = _read_runtime_log_sink() if stream is None else None
            handler.name = APPLICATION_LOG_HANDLER_NAME
            root_logger.addHandler(handler)
        elif not isinstance(handler, _RuntimeHandler):
            raise RuntimeError(f"日志 handler 名称冲突: {APPLICATION_LOG_HANDLER_NAME}")
        elif stream is not None:
            handler.setStream(stream)
            handler.log_sink = None
        handler.setLevel(resolved_level)
        handler.setFormatter(_RuntimeFormatter(
            service=service, pretty=selected_pretty, contract=contract,
        ))
        handler.applied_revision = revision
        root_logger.setLevel(resolved_level)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uvicorn_logger = logging.getLogger(name)
            uvicorn_logger.handlers.clear()
            uvicorn_logger.setLevel(logging.NOTSET)
            uvicorn_logger.propagate = True
        return handler


def runtime_logging_state() -> RuntimeLoggingState:
    """读取实际 handler，不另存一份可变日志设置。"""
    with _CONFIGURATION_LOCK:
        root = logging.getLogger()
        handlers = [handler for handler in root.handlers
                    if handler.name == APPLICATION_LOG_HANDLER_NAME]
        if len(handlers) != 1 or not isinstance(handlers[0], _RuntimeHandler):
            raise RuntimeError("产品日志 handler 尚未唯一初始化")
        handler = handlers[0]
        formatter = handler.formatter
        if not isinstance(formatter, _RuntimeFormatter) or root.level != handler.level:
            raise RuntimeError("产品日志实际过滤与格式状态不一致")
        return RuntimeLoggingState(
            service=formatter.service, pid=os.getpid(),
            runtime_instance_id=_RUNTIME_INSTANCE_ID,
            applied_revision=handler.applied_revision,
            level=logging.getLevelName(handler.level).lower(), pretty=formatter.pretty,
            log_sink=_verify_runtime_log_sink(handler.log_sink),
        )


def apply_runtime_logging(
    *, runtime_instance_id: str, revision: str, expected_revision: str | None,
    level: str, pretty: bool,
) -> RuntimeLoggingState:
    """用实例身份与当前 revision 作 CAS；重放不能覆盖更新后的设置。"""
    with _CONFIGURATION_LOCK:
        current = runtime_logging_state()
        if runtime_instance_id != current.runtime_instance_id:
            raise RuntimeLoggingConflictError("日志目标运行实例已变化")
        contract = load_runtime_log_contract()
        normalized_level = level.strip().lower()
        if not revision.strip() or normalized_level not in contract.levels:
            raise ValueError("日志 revision 或 level 非法")
        if type(pretty) is not bool:
            raise TypeError("日志 pretty 必须是布尔值")
        if revision == current.applied_revision:
            if (normalized_level, pretty) != (current.level, current.pretty):
                raise RuntimeLoggingConflictError("同一日志 revision 的设置不一致")
            return current
        if expected_revision != current.applied_revision:
            raise RuntimeLoggingConflictError("日志当前 revision 已变化")
        configure_application_logging(
            service=current.service, level=normalized_level, pretty=pretty, stream=None, revision=revision,
        )
        logging.getLogger(__name__).debug("应用产品日志设置: revision=%s", revision)
        return runtime_logging_state()
