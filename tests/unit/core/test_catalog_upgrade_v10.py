"""原 catalog 版本的显式副本转换与冻结点 CAS，不代替完整 fork 产品验收。"""

from __future__ import annotations

import json
from contextlib import closing

import pytest

from app.core.session_catalog_store import (
    CatalogMaintenanceRequiredError,
    SessionCatalogStore,
    upgrade_v10,
)
from app.core.session_catalog_store.contracts import ForkCapturedPoint
from app.core.session_catalog_store.fork_source_capture import (
    _capture_request_preimage,
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
    path = store.database_path
    store.close()
    # 旧结构只在迁移测试装具中重建；执行/fork 的关键状态由产品验收另行生成。
    with closing(external_catalog_connection(path)) as connection:
        connection.execute("ALTER TABLE fork_source_captures DROP COLUMN source_point_json")
        connection.execute("PRAGMA user_version=10")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path, capture, state


@pytest.mark.parametrize("v10_catalog", ["preparing", "source_captured", "materialized", "aborted"], indirect=True)
def test_explicit_v10_conversion_preserves_source_and_frozen_requests(v10_catalog, captured_point, sessions_root):
    path, original, state = v10_catalog
    before = path.read_bytes()
    destination = path.with_name("converted.sqlite")
    with pytest.raises(CatalogMaintenanceRequiredError, match="拒绝自动迁移"):
        SessionCatalogStore(path, sessions_root)
    upgrade_v10.convert_catalog_v10(path, destination)
    assert path.read_bytes() == before
    with closing(SessionCatalogStore(destination, sessions_root)) as converted:
        record = converted.get_fork_source_capture(copy_operation_id=original.copy_operation_id)
        assert record.state == state
        assert record.request_json == original.request_json
        assert record.preimage_hash == original.preimage_hash
        assert record.captured_point == captured_point
        assert converted.connection.execute("PRAGMA user_version").fetchone()[0] == 11


def test_v10_conversion_failure_after_sql_preserves_original_and_removes_only_own_output(v10_catalog, monkeypatch):
    path, _, _ = v10_catalog
    before = path.read_bytes()
    destination = path.with_name("failed.sqlite")
    validate = upgrade_v10.require_catalog_schema

    def fail_after_conversion(connection, **kwargs):
        validate(connection, **kwargs)
        if kwargs["current"] == 11:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
            assert connection.execute("SELECT source_point_json FROM fork_source_captures").fetchone()[0] is not None
            raise RuntimeError("注入提交前失败")

    monkeypatch.setattr(upgrade_v10, "require_catalog_schema", fail_after_conversion)
    with pytest.raises(RuntimeError, match="注入提交前失败"):
        upgrade_v10.convert_catalog_v10(path, destination)
    assert path.read_bytes() == before and not destination.exists()
    destination.write_bytes(b"existing-output")
    with pytest.raises(FileExistsError):
        upgrade_v10.convert_catalog_v10(path, destination)
    assert destination.read_bytes() == b"existing-output"


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
        upgrade_v10.convert_catalog_v10(path, destination)
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
