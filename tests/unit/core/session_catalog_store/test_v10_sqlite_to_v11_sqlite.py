"""原 catalog 版本的显式副本转换与冻结点 CAS，不代替完整 fork 产品验收。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.core.session_catalog_store import (
    CatalogMaintenanceRequiredError,
    SessionCatalogStore,
)
from app.core.session_catalog_store.contracts import ForkCapturedPoint
from app.core.session_catalog_store.fork_source_capture import (
    _capture_request_preimage,
)
from app.core.session_catalog_store.migrations import (
    v10_sqlite_to_v11_sqlite as migration,
)
from tests.unit.core.test_session_catalog_store import (
    create_session,
    external_catalog_connection,
    sessions_root,
    store,
)

__all__ = ["external_catalog_connection", "sessions_root", "store"]


@pytest.fixture
def captured_point() -> ForkCapturedPoint:
    return ForkCapturedPoint(source_view_id="view/冻结", source_view_revision=17, anchor_item_id="item/cut")


@pytest.fixture
def capture(store: SessionCatalogStore, captured_point: ForkCapturedPoint):
    source, target = create_session(store), create_session(store)
    return store.reserve_fork_source_capture(
        copy_operation_id="conversion/capture", creation_key=None,
        source_session_id=source.node_id, source_thread_id=source.main_thread_id,
        target_session_id=target.node_id, target_thread_id=target.main_thread_id,
        request={**captured_point.model_dump(), "relationship": "detached", "checkpoint_ns": ""},
    )


@pytest.fixture
def v10_catalog(store, capture, captured_point, external_catalog_connection, request):
    state = getattr(request, "param", "materialized")
    if state in {"source_captured", "materialized"}:
        store.mark_fork_source_captured(capture, snapshot_sha256="1" * 64, marker_sha256="2" * 64, source_point=captured_point)
    if state in {"materialized", "aborted"}:
        store.finish_fork_source_capture(capture.copy_operation_id, state=state)
    capture = store.get_fork_source_capture(copy_operation_id=capture.copy_operation_id)
    current_path = store.database_path
    store.close()
    path = current_path.with_name("v10.sqlite")
    # 结构来自固定旧源码；当前 owner 只为字段转换测试提供合法行，不冒充旧版历史执行。
    with closing(external_catalog_connection(current_path)) as current:
        current.row_factory = sqlite3.Row
        tables = [row[0] for row in current.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
        )]
        rows = {table: [dict(row) for row in current.execute(f"SELECT * FROM {table}")] for table in tables}
    with closing(external_catalog_connection(path)) as connection:
        # 固定旧产品的 catalog 也以 WAL 运行。
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        fixtures = Path.cwd() / "tests/unit/core/session_catalog_store"
        connection.executescript((fixtures / "schema_v10.sql").read_text(encoding="utf-8"))
        # 原 owner 用 execute 保留索引 SQL 的末尾换行；不能用脚本分句改写 sqlite_master。
        for asset in ("schema_v10_queue_index.sql", "schema_v10_pending_index.sql"):
            connection.execute((fixtures / asset).read_text(encoding="utf-8"))
        # 旧 nodes 触发器要求先有导航 revision 单例。
        for table in ("catalog_metadata", *(table for table in tables if table != "catalog_metadata")):
            records = rows[table]
            for record in records:
                if table == "nodes":
                    assert record.pop("current_model_json") is None
                    record["current_provider_id"] = "v10-provider" if record["kind"] == "session" else None
                if table == "fork_source_captures":
                    record.pop("source_point_json")
                columns = ",".join(record)
                parameters = ",".join("?" for _ in record)
                connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({parameters})", tuple(record.values()))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path, capture, state


@pytest.mark.parametrize("v10_catalog", ["preparing", "source_captured", "materialized", "aborted"], indirect=True)
def test_explicit_v10_conversion_preserves_source_and_frozen_requests(v10_catalog, captured_point, sessions_root, external_catalog_connection):
    path, original, state = v10_catalog
    before = path.read_bytes()
    destination = path.with_name("converted.sqlite")
    with closing(external_catalog_connection(path)) as source:
        source_nodes = source.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
        assert "current_provider_id" in {row[1] for row in source.execute("PRAGMA table_info(nodes)")}
        assert "current_model_json" not in {row[1] for row in source.execute("PRAGMA table_info(nodes)")}
    with pytest.raises(CatalogMaintenanceRequiredError, match="拒绝自动迁移"):
        SessionCatalogStore(path, sessions_root)
    migration.convert_catalog_v10(path, destination)
    assert path.read_bytes() == before
    # 这是 v10→v11 的离线合同；当前 Store 已为 v12，不能用它打开 v11 输出。
    with closing(external_catalog_connection(destination)) as converted:
        migration.require_catalog_schema(converted, database_path=destination, current=11, database_label="converted catalog")
        migration.verify_catalog_sqlite_integrity(converted, database_path=destination, database_label="converted catalog")
        record = converted.execute(
            "SELECT state,request_json,preimage_hash,source_point_json FROM fork_source_captures "
            "WHERE copy_operation_id=?", (original.copy_operation_id,),
        ).fetchone()
        assert record == (
            state, original.request_json, original.preimage_hash,
            captured_point.frozen_json(
                preimage_hash=original.preimage_hash, snapshot_sha256=original.snapshot_sha256, marker_sha256=original.marker_sha256,
            ),
        )
        assert converted.execute("PRAGMA user_version").fetchone()[0] == 11
        assert converted.execute("SELECT * FROM nodes ORDER BY node_id").fetchall() == source_nodes
        assert "current_model_json" not in {row[1] for row in converted.execute("PRAGMA table_info(nodes)")}


def test_v10_conversion_failure_after_sql_preserves_original_and_removes_only_own_output(v10_catalog, monkeypatch):
    path, _, _ = v10_catalog
    before = path.read_bytes()
    destination = path.with_name("failed.sqlite")
    validate = migration.require_catalog_schema

    def fail_after_conversion(connection, **kwargs):
        validate(connection, **kwargs)
        if kwargs["current"] == 11:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
            assert connection.execute("SELECT source_point_json FROM fork_source_captures").fetchone()[0] is not None
            raise RuntimeError("注入提交前失败")

    monkeypatch.setattr(migration, "require_catalog_schema", fail_after_conversion)
    with pytest.raises(RuntimeError, match="注入提交前失败"):
        migration.convert_catalog_v10(path, destination)
    assert path.read_bytes() == before and not destination.exists()
    destination.write_bytes(b"existing-output")
    with pytest.raises(FileExistsError):
        migration.convert_catalog_v10(path, destination)
    assert destination.read_bytes() == b"existing-output"


def test_v10_conversion_rejects_current_model_schema_with_old_version_label(
    store, capture, external_catalog_connection,
):
    path = store.database_path
    store.close()
    with closing(external_catalog_connection(path)) as connection:
        connection.execute("PRAGMA user_version=10")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.read_bytes()
    destination = path.with_name("wrong-version.sqlite")
    with pytest.raises(CatalogMaintenanceRequiredError, match="缺少权威会话字段"):
        migration.convert_catalog_v10(path, destination)
    assert path.read_bytes() == before and not destination.exists()


def test_v10_conversion_rejects_revision_trigger_from_another_version(v10_catalog, external_catalog_connection):
    path, _, _ = v10_catalog
    with closing(external_catalog_connection(path)) as connection:
        connection.execute("DROP TRIGGER nodes_navigation_revision_update")
        connection.execute(migration.load_sql(
            "app.core.session_catalog_store", "migrations/nodes_navigation_revision_update.sql",
        ))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.read_bytes()
    destination = path.with_name("wrong-trigger.sqlite")
    with pytest.raises(CatalogMaintenanceRequiredError, match="缺少导航 revision triggers 或定义不符"):
        migration.convert_catalog_v10(path, destination)
    assert path.read_bytes() == before and not destination.exists()


@pytest.mark.parametrize("v10_catalog", ["source_captured", "materialized"], indirect=True)
@pytest.mark.parametrize("missing_field", ["source_view_id", "source_view_revision"])
def test_v10_conversion_rejects_captured_records_without_exact_old_point(
    v10_catalog, missing_field, external_catalog_connection,
):
    path, original, _ = v10_catalog
    request = json.loads(original.request_json)
    del request[missing_field]
    request_json, preimage_hash = _capture_request_preimage(request)
    with closing(external_catalog_connection(path)) as connection:
        connection.execute(
            "UPDATE fork_source_captures SET request_json=?, preimage_hash=?",
            (request_json, preimage_hash),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.read_bytes()
    destination = path.with_name("unprovable.sqlite")
    with pytest.raises(RuntimeError, match="缺少实际冻结 source point"):
        migration.convert_catalog_v10(path, destination)
    assert path.read_bytes() == before
    assert not destination.exists()


def test_capture_point_cas_cannot_change_after_marker_commit(store, capture, captured_point):
    frozen = store.mark_fork_source_captured(capture, snapshot_sha256="1" * 64, marker_sha256="2" * 64, source_point=captured_point)
    assert store.mark_fork_source_captured(capture, snapshot_sha256="1" * 64, marker_sha256="2" * 64, source_point=captured_point) == frozen
    changed = captured_point.model_copy(update={"source_view_revision": 18})
    with pytest.raises(RuntimeError, match="captured hash 冲突"):
        store.mark_fork_source_captured(capture, snapshot_sha256="1" * 64, marker_sha256="2" * 64, source_point=changed)
    assert store.get_fork_source_capture(copy_operation_id=capture.copy_operation_id) == frozen


def test_capture_point_tampering_is_rejected_after_restart(store, capture, captured_point, sessions_root, external_catalog_connection):
    record = store.mark_fork_source_captured(capture, snapshot_sha256="1" * 64, marker_sha256="2" * 64, source_point=captured_point)
    path = store.database_path
    store.close()
    raw = json.loads(record.source_point_json)
    raw["point"]["source_view_revision"] += 1
    with closing(external_catalog_connection(path)) as connection:
        connection.execute("UPDATE fork_source_captures SET source_point_json=?", (
            json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
        connection.commit()
    with (
        closing(SessionCatalogStore(path, sessions_root)) as opened,
        pytest.raises(CatalogMaintenanceRequiredError, match="冻结身份损坏"),
        opened.read_transaction() as connection,
    ):
        opened.verify_fork_source_captures(connection=connection)
