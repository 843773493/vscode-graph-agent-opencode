"""Fork 诊断故障装具；业务状态仍由各测试的真实 owner 产生。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core import failures
from app.services.business import session_context_fork_service
from app.services.business.workspace_resources import owner as workspace_owner


@pytest.fixture(params=[False, True], ids=["logging", "broken-logging"])
def fork_diagnostic_sink(
    production_fork_container: AppContainer, request: pytest.FixtureRequest,
) -> Iterator[tuple[Path, list[BaseException]] | None]:
    if not request.param:
        yield None
        return
    blocked = production_fork_container.workspace_root / "blocked-fork-diagnostics-log"
    blocked.write_bytes(b"keep obstruction")
    errors: list[BaseException] = []

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.exc_info and record.exc_info[1] is not None:
                errors.append(record.exc_info[1])
            with (blocked / "service.log").open("a", encoding="utf-8"):
                pass

    handler = FailingHandler(level=logging.ERROR)
    loggers = (session_context_fork_service.logger, workspace_owner.logger, failures.logger)
    for logger in loggers:
        logger.addHandler(handler)
    try:
        yield blocked, errors
    finally:
        for logger in loggers:
            logger.removeHandler(handler)
        handler.close()
        assert blocked.read_bytes() == b"keep obstruction"
