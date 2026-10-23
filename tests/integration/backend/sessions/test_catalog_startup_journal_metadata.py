"""真实创建/删除产生 journal，冷启动拒绝无法按原操作恢复的记账字段。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from app.container import AppContainer
from app.core.session_catalog_store import (
    CatalogMaintenanceRequiredError,
    SessionCreationRecord,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
    _create_record,
)
from tests.integration.backend.sessions.test_catalog_startup_delete_records import (
    _prepare_delete,
)
from tests.integration.backend.sessions.test_catalog_startup_directories import (
    _catalog_dump,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)


def _assert_body_unchanged(container: AppContainer, files: dict[str, bytes]) -> None:
    root = container.workspace_root / ".boxteam"
    assert {str(p.relative_to(root)): p.read_bytes() for directory in ("sessions", "orphaned")
            for p in (root / directory).rglob("*") if p.is_file()} == files


@pytest.mark.parametrize("owner,state,column,value", [
    ("creation", "preparing", "session_creation_idempotency_key", ""),
    ("creation", "published", "session_creation_idempotency_key", None),
    ("delete", "deleting", "subtree_delete_idempotency_key", ""),
    ("delete", "completed", "subtree_delete_idempotency_key", None),
    ("creation", "published", "record_created_at", "invalid-time"),
    ("creation", "preparing", "record_updated_at", "2026-10-23T00:00:00"),
    ("delete", "completed", "record_created_at", "2026-10-23T00:00:00"),
    ("delete", "draining", "record_updated_at", "invalid-time"),
    ("creation", "aborted", "abort_reason", None),
    ("creation", "aborted", "abort_reason", ""),
    ("delete", "aborted", "abort_reason", None),
    ("delete", "aborted", "abort_reason", ""),
    ("creation", "published", "abort_reason", "尚未中止却有原因"),
    ("delete", "completed", "abort_reason", "已完成却有中止原因"),
])
async def test_container_rejects_unrecoverable_journal_metadata(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, owner: str, state: str, column: str, value: object,
) -> None:
    container = startup_container
    store = container.session_service.path_resolver.catalog_store
    if owner == "creation":
        record = await _create_record(container, monkeypatch, state)
        table, key_column, key = "session_creation_records", "session_creation_idempotency_key", record.session_creation_idempotency_key
    else:
        _, _, key = await _prepare_delete(container, monkeypatch, "preparing" if state == "aborted" else state)
        if state == "aborted":
            store.abort_subtree_delete(key, "显式终结原意图")
        table, key_column = "subtree_delete_records", "subtree_delete_idempotency_key"
    path, files = _close_and_files(container)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"UPDATE {table} SET {column}=? WHERE {key_column}=?", (value, key))
        connection.commit()
    before = _catalog_dump(path)
    with pytest.raises(CatalogMaintenanceRequiredError):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before
    _assert_body_unchanged(container, files)


async def _create_child_record(container: AppContainer) -> SessionCreationRecord:
    operations = container.session_catalog_service.operations
    await operations.start()
    try:
        parent_id = await container.session_catalog_service.ensure_folder_path(["冻结的创建父节点"])
    finally:
        await operations.stop()
    assert parent_id is not None
    session = await container.session_service.create(SessionCreateRequest(title="有父节点的创建意图", folder_id=parent_id))
    store = container.session_service.path_resolver.catalog_store
    with store.read_transaction() as connection:
        key = connection.execute(
            "SELECT session_creation_idempotency_key FROM session_creation_records WHERE session_id=?", (session.session_id,),
        ).fetchone()[0]
    return store.get_creation_record(key)


@pytest.mark.parametrize("has_parent,revision", [(False, 1), (True, None), (True, 0), (True, 1.5)])
async def test_container_rejects_invalid_frozen_parent_revision(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, has_parent: bool, revision: object,
) -> None:
    container = startup_container
    record = (await _create_child_record(container) if has_parent
              else await _create_record(container, monkeypatch, "published"))
    path, files = _close_and_files(container)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE session_creation_records SET parent_revision=? WHERE session_creation_idempotency_key=?",
            (revision, record.session_creation_idempotency_key),
        )
        connection.commit()
    before = _catalog_dump(path)
    with pytest.raises(CatalogMaintenanceRequiredError, match="creation record envelope"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before
    _assert_body_unchanged(container, files)


async def test_container_preserves_valid_journal_times_after_wall_clock_change(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    container = startup_container
    record = await _create_child_record(container)
    path, files = _close_and_files(container)
    # 只校记账时间的解析合同；墙钟回拨不能冒充非法状态转换或覆盖原时间。
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE session_creation_records SET record_updated_at=? WHERE session_creation_idempotency_key=?",
            ("2020-01-01T03:00:00+03:00", record.session_creation_idempotency_key),
        )
        connection.commit()
    before = _catalog_dump(path)
    _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before
    _assert_body_unchanged(container, files)
