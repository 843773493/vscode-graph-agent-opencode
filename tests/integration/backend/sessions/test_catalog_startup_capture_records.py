"""fork capture 的局部冻结身份合同，完整恢复仍由真实执行/fork 测试验收。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from app.container import AppContainer
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
)
from tests.integration.backend.sessions.test_catalog_startup_directories import (
    _catalog_dump,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_catalog_startup_journal_metadata import (
    _assert_body_unchanged,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)


async def _reserve_capture(container: AppContainer, state: str) -> ForkSourceCaptureRecord:
    service = container.session_service
    source = await service.create(SessionCreateRequest(title="冻结来源身份"))
    target = await service.create(SessionCreateRequest(title="冻结目标身份"))
    resolver = service.path_resolver
    store = resolver.catalog_store
    target_thread = resolver.main_thread_id(target.session_id)
    record = store.reserve_fork_source_capture(
        copy_operation_id="capture/startup", creation_key=None,
        source_session_id=source.session_id, source_thread_id=resolver.main_thread_id(source.session_id),
        target_session_id=target.session_id, target_thread_id=target_thread,
        request={"relationship": "detached", "checkpoint_ns": "", "target_main_thread_id": target_thread, "label": "冻结请求"},
    )
    if state in {"source_captured", "materialized"}:
        record = store.mark_fork_source_captured(record, snapshot_sha256="1" * 64, marker_sha256="2" * 64)
    if state in {"materialized", "aborted"}:
        store.finish_fork_source_capture(record.copy_operation_id, state=state)
    result = store.get_fork_source_capture(copy_operation_id=record.copy_operation_id)
    assert result is not None and result.state == state
    return result


@pytest.mark.parametrize("state,column,value", [
    ("preparing", "copy_operation_id", ""),
    ("aborted", "copy_operation_id", None),
    ("source_captured", "source_session_id", "ses-invalid-source"),
    ("materialized", "source_thread_id", "thr-invalid-source"),
    ("aborted", "target_session_id", "ses-invalid-target"),
    ("preparing", "target_thread_id", "thr-invalid-target"),
    ("source_captured", "snapshot_id", "invalid-snapshot"),
    ("materialized", "snapshot_relative_locator", "fork-source-captures/another-snapshot"),
    ("aborted", "fork_id", ""),
    ("preparing", "materialization_id", ""),
    ("materialized", "creation_key", ""),
    ("aborted", "request_json", "{broken"),
    ("preparing", "request_json", "[]"),
    ("source_captured", "request_json", "noncanonical"),
    ("materialized", "preimage_hash", "0" * 64),
])
async def test_container_rejects_corrupt_fork_capture_identity(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, state: str, column: str, value: object,
) -> None:
    container = startup_container
    record = await _reserve_capture(container, state)
    path, files = _close_and_files(container)
    with closing(sqlite3.connect(path)) as connection:
        if value == "noncanonical":
            value = json.dumps(json.loads(record.request_json), ensure_ascii=False, indent=2)
            # 内容与其自身 hash 都有效，仍不能接受非 writer 合同的编码字节。
            connection.execute("UPDATE fork_source_captures SET preimage_hash=? WHERE copy_operation_id=?", (hashlib.sha256(value.encode()).hexdigest(), record.copy_operation_id))
        connection.execute(f"UPDATE fork_source_captures SET {column}=? WHERE copy_operation_id=?", (value, record.copy_operation_id))
        connection.commit()
    before = _catalog_dump(path)
    with pytest.raises(CatalogMaintenanceRequiredError, match="fork source capture 冻结身份损坏"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before
    _assert_body_unchanged(container, files)


@pytest.mark.parametrize("state", ["preparing", "source_captured", "materialized", "aborted"])
async def test_container_preserves_frozen_capture_after_source_navigation_changes(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, state: str,
) -> None:
    container = startup_container
    record = await _reserve_capture(container, state)
    resolver = container.session_service.path_resolver
    resolver.catalog_store.rename_node(record.source_session_id, "来源合法改名")
    resolver.catalog_store.rename_node(record.target_session_id, "目标合法改名")
    if state in {"materialized", "aborted"}:
        await resolver.delete_subtree(idempotency_key="delete-detached-capture-source", root_node_id=record.source_session_id)
    path, files = _close_and_files(container)
    before = _catalog_dump(path)
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    assert restarted.session_service.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=record.copy_operation_id) == record
    assert _catalog_dump(path) == before
    _assert_body_unchanged(container, files)
