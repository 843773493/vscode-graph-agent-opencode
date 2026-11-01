"""复用原删除恢复场景，验证日志次错不阻止原回执结算。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest

from app.services.business.session_navigation import executor
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 — 真实日志写入失败。
)
from tests.unit.services.business.session_navigation.test_operations_queue import (
    navigation_workspace as navigation_workspace,  # noqa: PLC0414 — 独立工作区。
)
from tests.unit.services.business.session_navigation.test_operations_queue import (
    test_delete_mark_commits_before_physical_drain_and_recovers_settlement as test_delete_mark_commits_before_physical_drain_and_recovers_settlement,  # noqa: PLC0414 — 原完整行为断言。
)


@pytest.fixture(autouse=True, params=[False, True], ids=["diagnostics", "sink-failure"])
def settlement_diagnostics(
    request: pytest.FixtureRequest, diagnostic_sink: DiagnosticFailure,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    error = executor.logger.error
    originals: list[BaseException] = []
    with pytest.raises(OSError) as expected:
        diagnostic_sink.destination.write_text("目标平台的原生日志故障", encoding="utf-8")

    def observe(
        message: object, *args: object, exc_info: BaseException,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        originals.append(exc_info)
        diagnostic_sink.primary = exc_info if request.param else None
        error(message, *args, exc_info=exc_info, extra=extra)

    monkeypatch.setattr(executor.logger, "error", observe)
    yield
    assert len(originals) >= 2, "必须同时覆盖首次结算与原 worker 启动恢复"
    if not request.param:
        assert diagnostic_sink.errors == []
        return
    assert len(diagnostic_sink.errors) == len(diagnostic_sink.records) == len(originals)
    for primary, record, secondary in zip(originals, diagnostic_sink.records, diagnostic_sink.errors, strict=True):
        assert record.exc_info is not None and record.exc_info[1] is primary
        assert type(secondary) is type(expected.value) and secondary.errno == expected.value.errno
        assert secondary.filename == str(diagnostic_sink.destination)
        assert any(str(diagnostic_sink.destination) in note and type(secondary).__name__ in note
                   for note in primary.__notes__)
