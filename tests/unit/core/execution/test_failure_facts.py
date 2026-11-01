"""执行失败区分明确业务说明、系统 I/O 与上游原始诊断。"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core import failures
from app.core.failures import (
    execution_failure_message,
    is_provider_failure,
    mark_provider_failure,
    report_failure,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414
)
from tests.support.diagnostic_failures import (
    sqlite_failure as sqlite_failure,  # noqa: PLC0414
)


@pytest.mark.parametrize("error", [RuntimeError("没有活动会话"), TimeoutError("任务总期限已到")])
def test_local_business_failure_keeps_its_message(error: BaseException) -> None:
    assert not is_provider_failure(error)
    assert execution_failure_message(error) == str(error)


@pytest.mark.parametrize("sink_fails", [False, True], ids=["log-ok", "log-failed"])
def test_owner_diagnostic_logger_keeps_context_and_original_error(
    caplog: pytest.LogCaptureFixture, diagnostic_sink: DiagnosticFailure, sink_fails: bool,
) -> None:
    owner_logger = logging.getLogger("test.owner_failure")
    original = PermissionError(13, "原生读取失败", "/private/owner-record")
    if sink_fails:
        diagnostic_sink.primary = original
    with caplog.at_level(logging.ERROR):
        message = report_failure(
            original, detail="owner读取失败: errno=13", diagnostic_logger=owner_logger,
            extra={"request_id": "owner-request", "operation_id": "original-operation"},
        )
    record, = caplog.records
    assert record.name == owner_logger.name
    assert record.exc_info is not None and record.exc_info[1] is original
    assert record.__dict__["request_id"] == "owner-request"
    assert record.__dict__["operation_id"] == "original-operation"
    assert message == "owner读取失败: errno=13" + (
        ", diagnostic_sink_error=NotADirectoryError" if sink_fails else ""
    )
    if sink_fails:
        secondary, = diagnostic_sink.errors
        assert str(secondary) in "\n".join(original.__notes__)


@pytest.mark.parametrize("error", [
    OSError(5, "本机读取失败", "/private/execution.sqlite"),
    OSError("本机读取失败: /private/execution.sqlite"),
    PermissionError(r"本机读取失败: C:\private\execution.sqlite"),
    ConnectionError(r"本机连接失败: \\server\private\execution.sqlite"),
    TimeoutError(110, "本机读取超时", "/private/execution.sqlite"),
])
def test_system_io_failure_keeps_native_facts_without_filename(error: OSError) -> None:
    assert execution_failure_message(error) == (
        f"执行系统 I/O 失败: error={type(error).__name__}, errno={error.errno}"
    )


@pytest.mark.parametrize("sink_fails", [False, True], ids=["log-ok", "log-failed"])
def test_sqlite_failure_keeps_code_and_original_diagnostic_without_database_path(
    sqlite_failure: sqlite3.OperationalError, caplog: pytest.LogCaptureFixture,
    diagnostic_sink: DiagnosticFailure, sink_fails: bool,
) -> None:
    error = sqlite_failure
    original = (error.args, error.sqlite_errorcode, error.sqlite_errorname)
    expected = "执行数据库失败: error=OperationalError, sqlite_errorcode=14"
    if sink_fails:
        diagnostic_sink.primary = error
        expected += ", diagnostic_sink_error=NotADirectoryError"
    with caplog.at_level(logging.ERROR):
        message = execution_failure_message(error)
        assert caplog.records == []
        assert report_failure(error, detail=message) == expected
    record, = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is error
    assert str(error) in caplog.text and "sqlite-execution-private" not in expected
    assert (error.args, error.sqlite_errorcode, error.sqlite_errorname) == original
    if sink_fails:
        sink_error, = diagnostic_sink.errors
        assert str(sink_error) in "\n".join(error.__notes__)
        assert str(diagnostic_sink.destination) not in expected


@pytest.mark.parametrize("error", [
    sqlite3.DatabaseError("database refused: /private/execution.sqlite"),
    sqlite3.IntegrityError(r"database refused: C:\private\execution.sqlite"),
    sqlite3.OperationalError(r"database refused: \\target\private\execution.sqlite"),
])
def test_sqlite_failure_without_native_code_does_not_copy_diagnostic_text(error: sqlite3.Error) -> None:
    original = error.args
    assert execution_failure_message(error) == f"执行数据库失败: error={type(error).__name__}, sqlite_errorcode=None"
    assert error.args == original


def test_provider_sqlite_failure_keeps_provider_ownership(
    sqlite_failure: sqlite3.OperationalError, caplog: pytest.LogCaptureFixture,
) -> None:
    assert mark_provider_failure(sqlite_failure) is sqlite_failure
    with caplog.at_level(logging.ERROR):
        message = execution_failure_message(sqlite_failure)
    assert message == "模型执行失败: stage=execution-provider, error=OperationalError, sqlite_errorcode=14"
    record, = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is sqlite_failure
    assert str(sqlite_failure) in caplog.text


def test_provider_failure_keeps_instance_and_full_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    cause = OSError(13, "读取上游诊断", "/private/provider.sqlite")
    error = RuntimeError("上游返回: /private/provider.sqlite")
    error.__cause__ = cause
    assert mark_provider_failure(error) is error and is_provider_failure(error)
    with caplog.at_level(logging.ERROR):
        message = execution_failure_message(error)
    assert message == "模型执行失败: stage=execution-provider, error=RuntimeError"
    record, = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is error
    assert error.__cause__ is cause and str(error) in caplog.text and str(cause) in caplog.text


@pytest.fixture
def validation_failure() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(dict[str, int]).validate_python({
            "/private/validation-key.sqlite": "/private/validation-input.sqlite",
            r"C:\private\validation-key.sqlite": r"C:\private\validation-input.sqlite",
            r"\\target\private\validation-key.sqlite": r"\\target\private\validation-input.sqlite",
        })
    return caught.value


def test_internal_validation_failure_keeps_raw_input_only_in_original_diagnostic(
    validation_failure: ValidationError, caplog: pytest.LogCaptureFixture,
) -> None:
    error = validation_failure
    original = error.errors(include_url=False)
    message = execution_failure_message(error)
    assert message == "执行数据校验失败: error=ValidationError"
    with caplog.at_level(logging.ERROR):
        assert report_failure(error, detail=message) == message
    record, = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is error
    assert str(error) in caplog.text
    assert error.errors(include_url=False) == original
    assert "validation-key.sqlite" not in message and "validation-input.sqlite" not in message


def test_internal_validation_failure_survives_native_diagnostic_failure(
    validation_failure: ValidationError, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = tmp_path / "validation-log-secret"
    blocker.write_text("普通文件不能承载日志目录", encoding="utf-8")
    destination = blocker / "service.log"
    sink_errors: list[OSError] = []

    def reject_log(*_args: object, **_kwargs: object) -> None:
        try:
            destination.write_text("原始诊断", encoding="utf-8")
        except OSError as error:
            sink_errors.append(error)
            raise

    monkeypatch.setattr(failures.logger, "error", reject_log)
    original = validation_failure.errors(include_url=False)
    message = report_failure(validation_failure, detail=execution_failure_message(validation_failure))
    sink_error, = sink_errors
    assert message == f"执行数据校验失败: error=ValidationError, diagnostic_sink_error={type(sink_error).__name__}"
    assert validation_failure.errors(include_url=False) == original
    assert str(destination) in "\n".join(validation_failure.__notes__)
    assert "validation-log-secret" not in message


def test_provider_validation_failure_keeps_provider_ownership(
    validation_failure: ValidationError, caplog: pytest.LogCaptureFixture,
) -> None:
    assert mark_provider_failure(validation_failure) is validation_failure
    with caplog.at_level(logging.ERROR):
        message = execution_failure_message(validation_failure)
    assert message == "模型执行失败: stage=execution-provider, error=ValidationError"
    record, = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is validation_failure


@pytest.fixture(params=[
    "/private/canonical-field.sqlite", r"C:\private\canonical-field.sqlite", r"\\target\private\canonical-field.sqlite",
])
def canonical_failure(request: pytest.FixtureRequest) -> ItemSchemaError:
    with pytest.raises(ItemSchemaError) as caught:
        canonical_json_bytes({request.param: float("nan")})
    assert request.param in str(caught.value)
    return caught.value


@pytest.mark.parametrize("sink_fails", [False, True], ids=["log-ok", "log-failed"])
def test_canonical_validation_does_not_publish_dynamic_field_values(
    canonical_failure: ItemSchemaError, diagnostic_sink: DiagnosticFailure,
    caplog: pytest.LogCaptureFixture, sink_fails: bool,
) -> None:
    error = canonical_failure
    original = (error.args, error.__cause__, error.__context__)
    expected = "执行数据校验失败: error=ItemSchemaError"
    if sink_fails:
        diagnostic_sink.primary = error
        expected += ", diagnostic_sink_error=NotADirectoryError"
    message = execution_failure_message(error)
    assert caplog.records == []
    assert "canonical-field.sqlite" not in message
    with caplog.at_level(logging.ERROR):
        assert report_failure(error, detail=message) == expected
    record, = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is error
    assert str(error) in caplog.text
    assert (error.args, error.__cause__, error.__context__) == original
    if sink_fails:
        assert len(diagnostic_sink.errors) == 1
        assert str(diagnostic_sink.destination) in "\n".join(error.__notes__)


def test_provider_canonical_validation_keeps_provider_ownership(
    canonical_failure: ItemSchemaError, caplog: pytest.LogCaptureFixture,
) -> None:
    error = canonical_failure
    assert mark_provider_failure(error) is error
    with caplog.at_level(logging.ERROR):
        message = execution_failure_message(error)
    assert message == "模型执行失败: stage=execution-provider, error=ItemSchemaError"
    record, = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is error
