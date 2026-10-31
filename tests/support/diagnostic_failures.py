"""原生 SQLite 错误与真实日志写入故障的共享装具。"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@pytest.fixture
def sqlite_failure(tmp_path: Path) -> sqlite3.OperationalError:
    """从真实 SQLite 取得含原生位置的失败，供各公开错误消费者复用。"""
    destination = tmp_path / "sqlite-execution-private" / "source.sqlite"
    with closing(sqlite3.connect(":memory:")) as connection:
        with pytest.raises(sqlite3.OperationalError) as caught:
            connection.execute("ATTACH DATABASE ? AS original_native", (str(destination),))
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN
        assert str(destination) in str(caught.value)
        assert connection.execute("PRAGMA database_list").fetchall() == [(0, "main", "")]
    assert not destination.parent.exists()
    return caught.value


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
