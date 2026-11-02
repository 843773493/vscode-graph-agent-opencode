"""主题错误的诊断 sink 与业务错误独立失败。"""

from __future__ import annotations

import errno
import logging

import pytest


@pytest.fixture(params=[False, True], ids=["diagnostic-ok", "diagnostic-failed"])
def asset_diagnostics(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    records: list[logging.LogRecord] = []
    sink_error = PermissionError(errno.EACCES, "诊断写入被拒绝", "/private/theme-diagnostic-secret") if request.param else None

    class Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)
            if sink_error is not None:
                raise sink_error

    # 同时捕获原 owner 与共用出口，反例不依赖错误最终由哪一个 logger 发布。
    for name in ("app.gateway.theme.assets", "app.core.failures"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", [Sink()])
        monkeypatch.setattr(logger, "propagate", False)
        monkeypatch.setattr(logger, "level", logging.ERROR)
    return records, sink_error
