"""catalog 运行期拒绝只公布逻辑事实，原始位置保留在诊断。"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core import failures
from app.core.session_catalog_store import (
    CatalogMaintenanceRequiredError,
    SessionCatalogStore,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SessionCatalogStore]:
    catalog = SessionCatalogStore(tmp_path / "navigation/session-catalog.sqlite", tmp_path / "sessions")
    try:
        yield catalog
    finally:
        catalog.close()


@pytest.fixture(params=[False, True], ids=["diagnostics", "sink-failure"])
def sink_failure(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> OSError | None:
    if not request.param:
        return None
    error = OSError("诊断输出失败 /private/catalog-log")

    def reject(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(failures.logger, "error", reject)
    return error


@pytest.mark.parametrize("fault", ["closed", "rollback"])
@pytest.mark.parametrize("access", ["connection", "read", "write"])
def test_rejected_access_preserves_fault_without_publishing_database_path(
    store: SessionCatalogStore, sink_failure: OSError | None,
    caplog: pytest.LogCaptureFixture, fault: str, access: str,
) -> None:
    path = str(store.database_path)
    native = sqlite3.OperationalError(f"rollback 拒绝 {path}")
    native.sqlite_errorcode = sqlite3.SQLITE_IOERR
    native.sqlite_errorname = "SQLITE_IOERR"
    store.close()
    if fault == "rollback":
        store._rollback_failure = native
    caplog.set_level(logging.ERROR)
    with pytest.raises(RuntimeError, match="已关闭") as caught:
        if access == "connection":
            _ = store.connection
        else:
            transaction = store.read_transaction() if access == "read" else store.write_transaction()
            with transaction:
                pytest.fail("被拒绝的访问不能进入事务体")
    public = str(caught.value)
    assert path not in public and str(store.sessions_root) not in public
    assert "artifact=session_catalog" in public and "stage=catalog-" in public
    cause = caught.value.__cause__
    assert cause is not None and path in str(cause)
    if fault == "rollback":
        assert cause is native and "sqlite_errorcode=10" in public
    if sink_failure is None:
        assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    else:
        assert "diagnostic_sink_error=OSError" in public
        assert "/private/catalog-log" not in public
        assert any("/private/catalog-log" in note for note in cause.__notes__)
    assert store._closed


@pytest.mark.parametrize("coordinate", ["generation", "navigation_revision"])
def test_missing_metadata_is_not_repaired_or_exposed_as_a_path(
    store: SessionCatalogStore, sink_failure: OSError | None,
    caplog: pytest.LogCaptureFixture, coordinate: str,
) -> None:
    connection = store.connection
    connection.execute("DELETE FROM catalog_metadata WHERE singleton_id=1")
    before = tuple(connection.iterdump())
    caplog.set_level(logging.ERROR)
    read = store.current_generation if coordinate == "generation" else store.current_navigation_revision
    with pytest.raises(CatalogMaintenanceRequiredError) as caught:
        read()
    public = str(caught.value)
    assert str(store.database_path) not in public
    assert "artifact=session_catalog" in public and "stage=catalog-" in public
    assert coordinate.replace("_", " ") in public
    cause = caught.value.__cause__
    assert cause is not None and str(store.database_path) in str(cause)
    if sink_failure is None:
        assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    else:
        assert "diagnostic_sink_error=OSError" in public
        assert any("/private/catalog-log" in note for note in cause.__notes__)
    assert not connection.in_transaction
    assert tuple(connection.iterdump()) == before
    with pytest.raises(CatalogMaintenanceRequiredError, match="拒绝提交写事务"), store.write_transaction():
        pass
    assert not connection.in_transaction and tuple(connection.iterdump()) == before
