"""执行失败区分明确业务说明、系统 I/O 与上游原始诊断。"""

from __future__ import annotations

import logging

import pytest

from app.core.failures import (
    execution_failure_message,
    is_provider_failure,
    mark_provider_failure,
)


@pytest.mark.parametrize("error", [RuntimeError("没有活动会话"), TimeoutError("任务总期限已到")])
def test_local_business_failure_keeps_its_message(error: BaseException) -> None:
    assert not is_provider_failure(error)
    assert execution_failure_message(error) == str(error)


def test_system_io_failure_keeps_native_facts_without_filename() -> None:
    error = OSError(5, "本机读取失败", "/private/execution.sqlite")
    assert execution_failure_message(error) == "执行系统 I/O 失败: error=OSError, errno=5"


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
