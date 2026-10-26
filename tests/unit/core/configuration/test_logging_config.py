from __future__ import annotations

import io
import json
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.logging_config import (
    APPLICATION_LOG_HANDLER_NAME,
    RuntimeLogRecord,
    configure_application_logging,
    format_runtime_log_record,
)
from app.core.logging_contract import load_runtime_log_contract
from app.core.trace_middleware import reset_current_request_id, set_current_request_id


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


@pytest.mark.parametrize("pretty", [True, False])
def test_python_and_js_share_exact_record_samples(pretty: bool) -> None:
    samples = json.loads(Path("resources/logging/record-samples.json").read_text())
    for sample in samples:
        assert format_runtime_log_record(
            RuntimeLogRecord(**sample["record"]), pretty=pretty,
            contract=load_runtime_log_contract(),
        ) == sample["pretty" if pretty else "compact"]


@pytest.mark.parametrize("pretty", [True, False])
def test_real_logging_entry_has_process_identity_and_no_invented_owner(
    isolated_root_logger: logging.Logger, pretty: bool,
) -> None:
    stream = io.StringIO()
    handler = configure_application_logging(service="backend", pretty=pretty, stream=stream)
    logging.getLogger("app.test").info("中文 DEBUG 正文\n第二行")
    rendered = stream.getvalue()
    assert handler.name == APPLICATION_LOG_HANDLER_NAME
    assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", rendered)
    assert f"service=backend pid={os.getpid()} runtime_instance_id=" in rendered
    assert "logger=app.test" in rendered
    assert "中文 DEBUG 正文\n第二行\n" in rendered
    assert not any(f"{field}=" in rendered for field in (
        "workspace_id", "session_id", "thread_id", "request_id",
    ))


@pytest.mark.parametrize("level,expected", [
    (" DEBUG ", ["debug", "info", "warning", "error", "critical"]),
    ("info", ["info", "warning", "error", "critical"]),
    (logging.WARNING, ["warning", "error", "critical"]),
    ("error", ["error", "critical"]),
    ("critical", ["critical"]),
])
def test_logging_filters_all_five_levels(
    isolated_root_logger: logging.Logger, level: str | int, expected: list[str],
) -> None:
    stream = io.StringIO()
    configure_application_logging(service="gateway", level=level, pretty=False, stream=stream)
    logger = logging.getLogger("app.test")
    for name in ("debug", "info", "warning", "error", "critical"):
        getattr(logger, name)(f"诊断 {name}")
    assert re.findall(r"level=(\w+)", stream.getvalue()) == expected


def test_context_and_exception_keep_original_stack_without_leaking_request_scope(
    isolated_root_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    configure_application_logging(service="gateway", stream=stream)
    token = set_current_request_id("req-real")
    try:
        try:
            raise RuntimeError("原始异常 中文")
        except RuntimeError:
            logging.getLogger("app.test").exception(
                "请求失败", extra={"workspace_id": "workspace-real", "thread_id": "thread-real"},
            )
    finally:
        reset_current_request_id(token)
    output = stream.getvalue()
    assert 'workspace_id="workspace-real" thread_id="thread-real" request_id="req-real"' in output
    assert "Traceback (most recent call last):" in output
    assert "test_logging_config.py" in output
    assert "RuntimeError: 原始异常 中文" in output
    stream.seek(0)
    stream.truncate()
    logging.getLogger("app.test").info("作用域已结束")
    assert "request_id=" not in stream.getvalue()


def test_reconfiguration_preserves_instance_and_unifies_uvicorn_handlers(
    isolated_root_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    legacy = io.StringIO()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.StreamHandler(legacy)]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
    first = configure_application_logging(service="backend", stream=stream)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).info("单次 %s", name)
    second = configure_application_logging(service="backend", level="debug", stream=stream)
    logging.getLogger("uvicorn.error").debug("调整后")
    assert first is second
    assert [handler for handler in isolated_root_logger.handlers
            if handler.name == APPLICATION_LOG_HANDLER_NAME] == [first]
    assert isolated_root_logger.level == logging.DEBUG
    assert legacy.getvalue() == ""
    assert stream.getvalue().count("单次 ") == 3
    assert stream.getvalue().count("调整后") == 1
    identities = re.findall(r"runtime_instance_id=([0-9a-f]+)", stream.getvalue())
    assert len(identities) == 4
    assert len(set(identities)) == 1


@pytest.mark.parametrize("level", ["verbose", "", "warn", True, 15])
def test_application_logging_rejects_unknown_level(
    isolated_root_logger: logging.Logger, level: str | int,
) -> None:
    with pytest.raises(ValueError, match="不支持的日志级别"):
        configure_application_logging(service="backend", level=level)


@pytest.mark.parametrize("pretty", ["false", 1, [], {}])
def test_pretty_requires_boolean(
    isolated_root_logger: logging.Logger, pretty: object,
) -> None:
    with pytest.raises(TypeError, match="必须是布尔值"):
        configure_application_logging(service="backend", pretty=pretty)  # type: ignore[arg-type]


def test_sink_failure_raises_original_error_once(isolated_root_logger: logging.Logger) -> None:
    original = OSError("日志磁盘写入失败")

    class BrokenStream(io.StringIO):
        calls = 0

        def write(self, content: str) -> int:
            self.calls += 1
            raise original

    stream = BrokenStream()
    configure_application_logging(service="backend", stream=stream)
    with pytest.raises(OSError) as failure:
        logging.getLogger("app.test").error("不能静默失败")
    assert failure.value is original
    assert stream.calls == 1
