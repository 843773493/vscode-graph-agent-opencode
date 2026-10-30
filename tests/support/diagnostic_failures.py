"""在真实日志分发后注入原生写入故障，按原异常实例限定边界。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@dataclass
class DiagnosticFailure:
    destination: Path
    primary: BaseException | None = None
    errors: list[OSError] = field(default_factory=list)
    records: list[logging.LogRecord] = field(default_factory=list)


@pytest.fixture
def diagnostic_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DiagnosticFailure:
    blocker = tmp_path / "diagnostic-log-secret"
    blocker.write_text("普通文件不能作为日志目录")
    failure = DiagnosticFailure(blocker / "service.log")
    call_handlers = logging.Logger.callHandlers

    def emit(logger: logging.Logger, record: logging.LogRecord) -> None:
        call_handlers(logger, record)
        if record.exc_info is not None and record.exc_info[1] is failure.primary:
            failure.records.append(record)
            try:
                failure.destination.write_text(record.getMessage())
            except OSError as error:
                failure.errors.append(error)
                raise

    monkeypatch.setattr(logging.Logger, "callHandlers", emit)
    return failure
