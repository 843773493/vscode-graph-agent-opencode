"""启动及只读备份必须拒绝原库损坏，同时保留私有诊断。"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import (
    CatalogMaintenanceRequiredError,
    SessionCatalogStore,
)
from app.core.session_catalog_store.queries import _register_session_list_functions
from tests.unit.core.session_catalog_store.test_access_errors import (
    sink_failure as sink_failure,  # noqa: PLC0414
)
from tests.unit.core.session_catalog_store.test_access_errors import (
    store as store,  # noqa: PLC0414
)

_PRIVATE = "/private/catalog-native-detail"
_SCHEMA_FAULTS = (
    "missing-table", "authority-columns", "metadata-columns", "metadata-row",
    "source-point", "queue-index", "revision-trigger", "integrity-index", "foreign-key", "corrupt-file",
)


def _corrupt(path: Path, fault: str, node_id: str) -> None:
    if fault == "corrupt-file":
        path.write_bytes(b"not a sqlite database: " + _PRIVATE.encode())
        return
    with closing(sqlite3.connect(path)) as connection:
        _register_session_list_functions(connection)
        if fault == "missing-table":
            connection.execute("DROP TABLE fork_retention_claims")
        elif fault == "authority-columns":
            connection.execute(f'ALTER TABLE nodes RENAME COLUMN title_source TO "{_PRIVATE}"')
        elif fault == "metadata-columns":
            connection.execute(f'ALTER TABLE catalog_metadata RENAME COLUMN navigation_revision TO "{_PRIVATE}"')
        elif fault == "metadata-row":
            connection.execute("DELETE FROM catalog_metadata WHERE singleton_id=1")
        elif fault == "source-point":
            connection.execute(f'ALTER TABLE fork_source_captures RENAME COLUMN source_point_json TO "{_PRIVATE}"')
        elif fault == "queue-index":
            connection.execute("DROP INDEX idx_navigation_mutation_queue")
        elif fault == "revision-trigger":
            connection.execute("DROP TRIGGER nodes_navigation_revision_update")
        elif fault == "integrity-index":
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_master SET sql=replace(sql, 'nodes(parent_node_id)', 'nodes(display_name)') "
                "WHERE name='idx_nodes_parent'",
            )
        elif fault == "foreign-key":
            connection.execute("UPDATE nodes SET parent_node_id=? WHERE node_id=?", (create_prefixed_id("ses"), node_id))
        elif fault == "old-version":
            connection.execute("PRAGMA user_version=10")
        elif fault == "future-version":
            connection.execute("PRAGMA user_version=999")
        else:
            assert fault == "unversioned"
            connection.execute("PRAGMA user_version=0")
            connection.execute(f'CREATE TABLE "{_PRIVATE}" (value TEXT)')
        connection.commit()


@pytest.mark.parametrize("entry,fault", [
    *((entry, fault) for entry in ("startup", "backup") for fault in _SCHEMA_FAULTS),
    *(("startup", fault) for fault in ("old-version", "future-version", "unversioned")),
])
def test_maintenance_rejects_corruption_without_publishing_native_details(
    store: SessionCatalogStore, sink_failure: OSError | None,
    caplog: pytest.LogCaptureFixture, entry: str, fault: str,
) -> None:
    node_id = create_prefixed_id("ses")
    store.create_folder(node_id, "test-workspace", None, "维护前原件")
    if entry == "startup":
        path = store.database_path
        store.close()
        generation = None
    else:
        path = store.database_path.with_name("catalog-backup.sqlite")
        generation = store.create_consistent_backup(path).generation
    _corrupt(path, fault, node_id)
    before = path.read_bytes()
    caplog.set_level(logging.ERROR)
    expected = RuntimeError if entry == "startup" and fault in ("missing-table", "future-version") else CatalogMaintenanceRequiredError
    with pytest.raises(expected) as caught:
        if entry == "startup":
            with closing(SessionCatalogStore(path, store.sessions_root)):
                pytest.fail("损坏权威库不能打开")
        else:
            assert generation is not None
            store.verify_consistent_backup(
                path, expected_checksum=hashlib.sha256(before).hexdigest(), backup_generation=generation,
            )
    public = str(caught.value)
    assert type(caught.value) is expected
    assert str(path) not in public and _PRIVATE not in public
    assert "artifact=session_catalog" in public and "stage=catalog-" in public
    cause = caught.value.__cause__
    assert cause is not None
    chain: list[BaseException] = []
    while cause is not None:
        chain.append(cause)
        cause = cause.__cause__
    if sink_failure is None:
        assert str(path) in caplog.text
        assert any(record.exc_info and record.exc_info[1] in chain for record in caplog.records)
    else:
        assert "diagnostic_sink_error=OSError" in public
        assert "/private/catalog-log" not in public
        assert any("/private/catalog-log" in note for error in chain for note in getattr(error, "__notes__", ()))
    assert path.read_bytes() == before
    if entry == "backup":
        assert not Path(str(path) + "-wal").exists() and not Path(str(path) + "-shm").exists()
        assert store.get_node(node_id).display_name == "维护前原件"


@pytest.mark.parametrize("fault", ["same-file", "existing", "missing", "checksum", "stale", "invalid-create", "invalid-verify"])
def test_backup_preconditions_keep_native_details_in_diagnostics(
    store: SessionCatalogStore, sink_failure: OSError | None, caplog: pytest.LogCaptureFixture, fault: str,
) -> None:
    path = store.database_path.with_name("precondition-backup.sqlite")
    manifest = store.create_consistent_backup(path)
    original = path.read_bytes()
    if fault == "stale":
        store.create_folder(create_prefixed_id("ses"), "test-workspace", None, "较新的事实")
    if fault == "missing":
        path.unlink()
    generation = store.current_generation()
    caplog.set_level(logging.ERROR)
    expected = (
        ValueError if fault == "same-file" else TypeError if fault.startswith("invalid-")
        else RuntimeError if fault == "existing" else CatalogMaintenanceRequiredError
    )
    with pytest.raises(expected) as caught:
        if fault == "same-file":
            store.create_consistent_backup(store.database_path)
        elif fault == "existing":
            store.create_consistent_backup(path)
        elif fault == "invalid-create":
            store.create_consistent_backup(_PRIVATE)  # type: ignore[arg-type] — 验证原有类型拒绝入口。
        elif fault == "invalid-verify":
            store.verify_consistent_backup(_PRIVATE, expected_checksum=manifest.checksum, backup_generation=manifest.generation)  # type: ignore[arg-type] — 同一非法输入不得转为普通位置输出。
        else:
            store.verify_consistent_backup(
                path, expected_checksum=_PRIVATE if fault == "checksum" else manifest.checksum,
                backup_generation=manifest.generation,
            )
    public = str(caught.value)
    assert type(caught.value) is expected
    assert str(path) not in public and str(store.database_path) not in public and _PRIVATE not in public
    assert "artifact=session_catalog" in public and "stage=catalog-" in public
    cause = caught.value.__cause__
    assert cause is not None
    if sink_failure is None:
        assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    else:
        assert "diagnostic_sink_error=OSError" in public
        assert any("/private/catalog-log" in note for note in cause.__notes__)
    assert store.current_generation() == generation
    assert not path.exists() if fault == "missing" else path.read_bytes() == original
