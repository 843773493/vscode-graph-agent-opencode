"""真实导航事务生成的终态事件在冷启动和分页时必须保持原始合同。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_catalog_startup_creation_records import (
    _close_and_files,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
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
from tests.support.session_navigation_queue import (
    create_catalog_intent,
    submit_catalog_intent,
)


async def _create_event(container: AppContainer) -> str:
    await container.session_service.create(SessionCreateRequest(title="事件校验原件"))
    catalog = container.session_catalog_service
    record = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "create_folder", name="事件所属目录",
    ))
    assert record.state == "committed"
    return record.operation_id


def _rewrite_event(path: Path, operation_id: str, column: str, value: object) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            f"UPDATE navigation_events SET {column}=? WHERE operation_id=?",
            (value, operation_id),
        )
        connection.commit()


def _event_rows(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT * FROM navigation_events ORDER BY event_seq").fetchall()


@pytest.mark.parametrize("column,value", [
    ("workspace_id", "another-workspace"),
    ("gateway_id", "another-gateway"),
    ("actor", "another-actor"),
    ("operation_id", "invalid-operation"),
    ("operation_id", "op_" + "1" * 32),
    ("event_seq", 0),
    ("event_seq", 1.5),
    ("queue_seq", 1.5),
    ("queue_seq", 999),
    ("kind", "rename_node"),
    ("result_state", "queued"),
    ("result_state", "rejected"),
    ("committed_catalog_revision", -1),
    ("committed_catalog_revision", 999),
    ("affected_node_ids_json", '{"node":"wrong-shape"}'),
    ("affected_node_ids_json", '[123]'),
    ("affected_node_ids_json", '["same", "same"]'),
    ("created_at", "invalid-time"),
    ("created_at", "2026-10-23T00:00:00"),
])
async def test_startup_rejects_corrupt_navigation_event_before_worker_recovery(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, column: str, value: object,
) -> None:
    container = startup_container
    operation_id = await _create_event(container)
    path, files = _close_and_files(container)
    _rewrite_event(path, operation_id, column, value)
    before, raw = _catalog_dump(path), path.read_bytes()
    with pytest.raises(CatalogMaintenanceRequiredError, match="navigation event 原始记录损坏"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before and path.read_bytes() == raw
    _assert_body_unchanged(container, files)


@pytest.mark.parametrize("column,value", [
    ("affected_node_ids_json", '{"node":"wrong-shape"}'),
    ("queue_seq", 999),
])
async def test_live_event_page_rejects_external_corruption_in_same_read_snapshot(
    startup_container: AppContainer, column: str, value: object,
) -> None:
    container = startup_container
    operation_id = await _create_event(container)
    catalog = container.session_catalog_service
    await catalog.operations.start()
    try:
        path = catalog.path_resolver.catalog_store.database_path
        _rewrite_event(path, operation_id, column, value)
        before = _catalog_dump(path)
        with pytest.raises(CatalogMaintenanceRequiredError, match="navigation event"):
            catalog.navigation_events(after=0, limit=10)
        assert _catalog_dump(path) == before
    finally:
        await catalog.operations.stop()


async def test_healthy_event_history_survives_target_deletion_and_clock_rollback(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    container = startup_container
    catalog = container.session_catalog_service
    created = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "create_folder", name="随后删除的目录",
    ))
    renamed = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "rename_node", target_node_id=created.result_node_id, name="新目录名",
    ))
    deleted = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "delete_folder", target_node_id=created.result_node_id,
    ))
    rejected = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "delete_folder", target_node_id=created.result_node_id,
    ))
    assert rejected.state == "rejected"
    path, files = _close_and_files(container)
    # 事件是历史事实，既不要求目标仍存在，也不按墙钟顺序重排或改写。
    _rewrite_event(path, renamed.operation_id, "created_at", "2025-01-01T00:00:00+08:00")
    before = _event_rows(path)
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    await restarted.session_catalog_service.operations.start()
    try:
        first = restarted.session_catalog_service.navigation_events(after=0, limit=2)
        second = restarted.session_catalog_service.navigation_events(after=first.items[-1].event_seq, limit=2)
        events = first.items + second.items
        assert [item.operation_id for item in events] == [
            created.operation_id, renamed.operation_id, deleted.operation_id, rejected.operation_id,
        ]
        assert [item.event_seq for item in events] == [1, 2, 3, 4]
        assert [item.result_state for item in events] == ["committed"] * 3 + ["rejected"]
        assert events[1].created_at.isoformat() == "2025-01-01T00:00:00+08:00"
        assert events[-1].error_code == rejected.error_code
        assert events[-1].error_detail == rejected.error_detail
    finally:
        await restarted.session_catalog_service.operations.stop()
    # 启动worker会合法递增owner generation；事件和业务正文仍保持原件。
    assert _event_rows(path) == before
    _assert_body_unchanged(container, files)
