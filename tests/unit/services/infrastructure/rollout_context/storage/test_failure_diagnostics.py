"""复用原连接／锁行为断言，验证真实日志次错不会改变故障和准入。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.atomic_fs import native_io_path
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutSQLiteConnection,
)
from tests.unit.services.infrastructure.rollout_context.locking.test_failures import (
    test_failed_cleanup_close_is_reported_without_stranding_admission as test_failed_cleanup_close_is_reported_without_stranding_admission,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.locking.test_failures import (
    test_fatal_file_lock_failure_keeps_cause_and_closes_handle as test_fatal_file_lock_failure_keeps_cause_and_closes_handle,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.locking.test_failures import (
    test_file_timeout_keeps_reason_and_releases_admission as test_file_timeout_keeps_reason_and_releases_admission,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.locking.test_failures import (
    test_invalid_parent_does_not_strand_admission as test_invalid_parent_does_not_strand_admission,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.locking.test_failures import (
    test_local_timeout_withdraws_waiter_and_keeps_original_holder as test_local_timeout_withdraws_waiter_and_keeps_original_holder,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.locking.test_failures import (
    test_missing_read_lock_preserves_type_and_can_recover as test_missing_read_lock_preserves_type_and_can_recover,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.locking.test_failures import (
    test_symlink_loop_keeps_native_error_in_diagnostics as test_symlink_loop_keeps_native_error_in_diagnostics,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.locking.test_failures import (
    test_upgrade_rejection_keeps_reader_and_diagnostics as test_upgrade_rejection_keeps_reader_and_diagnostics,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.storage.test_connection_paths import (
    connection_mode as connection_mode,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.storage.test_connection_paths import (
    index_path as index_path,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.storage.test_connection_paths import (
    invalid_index as invalid_index,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.storage.test_connection_paths import (
    test_native_inspection_failure_preserves_type_and_recovers as test_native_inspection_failure_preserves_type_and_recovers,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)
from tests.unit.services.infrastructure.rollout_context.storage.test_connection_paths import (
    test_structural_rejection_preserves_source_and_does_not_open_sqlite as test_structural_rejection_preserves_source_and_does_not_open_sqlite,  # noqa: PLC0414 — pytest 收集原用例和依赖 fixture。
)

_LOGGER = "app.services.infrastructure.rollout_context.storage.primitives"


@pytest.fixture(autouse=True, params=[False, True], ids=["logging", "logging-unavailable"])
def storage_diagnostic_log(
    request: pytest.FixtureRequest, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> Iterator[list[OSError]]:
    blocker = tmp_path / "storage-diagnostic-secret"
    blocker.write_text("日志父目录是普通文件", encoding="utf-8")
    destination = blocker / "service.log"
    with pytest.raises(OSError) as expected:
        destination.write_text("目标平台日志失败原件", encoding="utf-8")
    records: list[logging.LogRecord] = []
    errors: list[OSError] = []
    emit = caplog.handler.emit

    def capture(record: logging.LogRecord) -> None:
        emit(record)
        if record.name != _LOGGER:
            return
        records.append(record)
        if request.param:
            try:
                destination.write_text(record.getMessage(), encoding="utf-8")
            except OSError as error:
                errors.append(error)
                raise

    monkeypatch.setattr(caplog.handler, "emit", capture)
    yield errors
    assert records
    assert blocker.read_text(encoding="utf-8") == "日志父目录是普通文件"
    assert len(errors) == (len(records) if request.param else 0)
    for error in errors:
        assert type(error) is type(expected.value)
        assert error.errno == expected.value.errno
        assert error.filename == str(destination)


def test_native_error_without_errno_keeps_public_stage_and_cause(
    index_path: Path, monkeypatch: pytest.MonkeyPatch, storage_diagnostic_log: list[OSError],
) -> None:
    failure = OSError(f"无 errno 的原生失败 {index_path}")
    original = Path.is_symlink

    def inspect(path: Path) -> bool:
        if path == native_io_path(index_path):
            raise failure
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_symlink", inspect)
        with pytest.raises(OSError) as caught:
            _RolloutSQLiteConnection.open(index_path, read_only=True, frozen_source=False)
    public = caught.value
    assert type(public) is OSError and public.__cause__ is failure
    assert public.errno is None and public.filename is None
    assert "artifact=rollout_index stage=path_validation error_type=OSError" in str(public)
    assert str(index_path) not in str(public)
    assert ("diagnostic_sink_error=" in str(public)) is bool(storage_diagnostic_log)
    if storage_diagnostic_log:
        sink_type = type(storage_diagnostic_log[0]).__name__
        assert f"diagnostic_sink_error={sink_type}" in str(public)
        assert any(sink_type in note for note in failure.__notes__)
    with _RolloutSQLiteConnection.open(index_path, read_only=True, frozen_source=False) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("原始正文 Ω",)
