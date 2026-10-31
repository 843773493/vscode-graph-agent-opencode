"""执行失败区分明确业务说明、系统 I/O 与上游原始诊断。"""

from __future__ import annotations

import logging
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
def test_system_io_failure_keeps_native_facts_without_filename(error: OSError) -> None:
    assert execution_failure_message(error) == (
        f"执行系统 I/O 失败: error={type(error).__name__}, errno={error.errno}"
    )


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
