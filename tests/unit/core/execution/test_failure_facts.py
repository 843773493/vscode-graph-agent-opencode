"""产品失败分类保持，公开原因与原诊断使用同一脱敏出口。"""

from __future__ import annotations

import logging
import sqlite3

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.failures import (
    execution_failure_message,
    failure_detail,
    is_provider_failure,
    mark_provider_failure,
    report_failure,
)
from tests.support.diagnostic_failures import (
    sqlite_failure as sqlite_failure,  # noqa: PLC0414
)


@pytest.mark.parametrize("error", [RuntimeError("没有活动会话"), TimeoutError("任务总期限已到")])
def test_local_business_failure_keeps_its_message(error: BaseException) -> None:
    assert not is_provider_failure(error)
    assert execution_failure_message(error) == str(error)


@pytest.mark.parametrize("error", [
    OSError(5, "本机读取失败", "/private/execution.sqlite"),
    OSError("本机读取失败: /private/execution.sqlite"),
    PermissionError(r"本机读取失败: C:\private\execution.sqlite"),
    ConnectionError(r"本机连接失败: \\server\private\execution.sqlite"),
    TimeoutError(110, "本机读取超时", "/private/execution.sqlite"),
])
def test_system_io_failure_keeps_original_classification(error: OSError) -> None:
    expected = f"执行系统 I/O 失败: error={type(error).__name__}, errno={error.errno}"
    if error.filename is not None:
        expected += f", reason={error.strerror}, filename={error.filename!r}"
    assert execution_failure_message(error) == expected


def test_sqlite_failure_keeps_code_and_original_diagnostic(
    sqlite_failure: sqlite3.OperationalError, caplog: pytest.LogCaptureFixture,
) -> None:
    original = (sqlite_failure.args, sqlite_failure.sqlite_errorcode, sqlite_failure.sqlite_errorname)
    message = execution_failure_message(sqlite_failure)
    assert message == "执行数据库失败: error=OperationalError, sqlite_errorcode=14, reason=" + str(sqlite_failure)
    with caplog.at_level(logging.ERROR):
        assert report_failure(sqlite_failure, detail=message) == message
    record, = caplog.records
    assert record.exc_info is None
    assert str(sqlite_failure) in record.getMessage()
    assert (sqlite_failure.args, sqlite_failure.sqlite_errorcode, sqlite_failure.sqlite_errorname) == original


def test_provider_failure_keeps_instance_cause_and_reason(caplog: pytest.LogCaptureFixture) -> None:
    cause = OSError(13, "读取上游诊断", "/private/provider.sqlite")
    error = RuntimeError("上游拒绝请求 password=private-value")
    error.__cause__ = cause
    assert mark_provider_failure(error, diagnostic_logger=logging.getLogger(__name__)) is error and is_provider_failure(error)
    with caplog.at_level(logging.ERROR):
        message = execution_failure_message(error)
    assert "stage=execution-provider" in message and "上游拒绝请求" in message
    assert "private-value" not in message and "private-value" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert error.__cause__ is cause and str(cause) in caplog.text


def test_validation_exposes_field_and_reason_but_no_input(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(dict[str, int]).validate_python({"dynamic.field": "private-input"})
    error = caught.value
    original = error.errors(include_url=False)
    message = execution_failure_message(error)
    assert "dynamic.field" in message and "int_parsing" in message
    with caplog.at_level(logging.ERROR):
        assert report_failure(error, detail=message) == message
    assert caplog.records[0].exc_info is None
    assert "private-input" not in message and "private-input" not in caplog.text
    assert error.errors(include_url=False) == original


def test_failure_detail_keeps_known_stage_and_unknown_reason(caplog: pytest.LogCaptureFixture) -> None:
    error = RuntimeError("未知类型的真实故障")
    with caplog.at_level(logging.ERROR):
        message = failure_detail(error, stage="plugin-start", label="插件启动失败")
    assert "插件启动失败" in message and "stage=plugin-start" in message
    assert "未知类型的真实故障" in message
    assert len(caplog.records) == 1 and caplog.records[0].exc_info is None


@pytest.fixture
def rejecting_logger(monkeypatch: pytest.MonkeyPatch) -> logging.Logger:
    selected = logging.getLogger("test.failure-rejected")

    def reject(*_args: object, **_kwargs: object) -> None:
        raise OSError("日志写入失败 password=private-value")

    monkeypatch.setattr(selected, "error", reject)
    return selected


def test_reporting_failure_preserves_primary_and_safe_secondary(rejecting_logger: logging.Logger) -> None:
    cause = ValueError("原始原因")
    error = RuntimeError("主失败")
    error.__cause__ = cause
    message = report_failure(error, detail="主失败", diagnostic_logger=rejecting_logger)
    assert message == "主失败, diagnostic_sink_error=OSError"
    assert error.__cause__ is cause
    assert "日志写入失败" in "\n".join(error.__notes__)
    assert "private-value" not in "\n".join(error.__notes__)


def test_same_credential_removed_from_public_and_diagnostic(caplog: pytest.LogCaptureFixture) -> None:
    error = RuntimeError("主原因 operation-private-value")
    error.__cause__ = ValueError("次生原因 operation-private-value")
    with caplog.at_level(logging.ERROR):
        message = report_failure(error, detail=str(error), secrets=("operation-private-value",))
    assert "主原因" in message and "次生原因" in caplog.text
    assert "operation-private-value" not in message and "operation-private-value" not in caplog.text


def test_provider_public_fact_budget_and_later_sink_failure_do_not_revisit_raw_cause(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from app.core.failures import public_error

    secret = "unit-original-provider-credential-683902"
    error = RuntimeError(("Incorrect API key provided: " + secret + "\n") * 1000)
    cause = ValueError("原上游cause: " + secret)
    error.__cause__ = cause
    mark_provider_failure(error, secrets=(secret,), diagnostic_logger=logging.getLogger(__name__))
    snapshot = public_error(error, code="\x01" * 10000, stage="\x01" * 10000)
    assert snapshot["details"]["truncated"] is True
    assert len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()) <= 16 * 1024
    assert secret not in json.dumps(snapshot, ensure_ascii=False) and secret not in caplog.text
    sink = logging.getLogger("provider-later-sink")

    def fail(*_args, **_kwargs):
        raise OSError(5, "日志写入失败")

    monkeypatch.setattr(sink, "error", fail)
    # 真实 except 上下文中 sink 异常会隐式引用原 Provider；后续不允许再遍历它。
    try:
        raise error
    except RuntimeError as original:
        detail = report_failure(original, detail="Provider 已失败", diagnostic_logger=sink)
    assert "diagnostic_sink_error=OSError" in detail
    assert secret not in error.__notes__[-1]
    assert "errno=5" in error.__notes__[-1]
    assert error.__cause__ is cause
