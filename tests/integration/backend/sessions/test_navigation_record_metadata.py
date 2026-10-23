"""真实导航回执的序号、fencing与settlement不能在读取或恢复时隐式转换。"""

from __future__ import annotations

import asyncio

import pytest

from app.container import AppContainer
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.core.sqlite_state import utc_now_text
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
)
from app.services.business.session_navigation.operations_service import (
    local_navigation_scope,
)
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
from tests.integration.backend.sessions.test_navigation_intent_integrity import (
    _committed_intent,
    _rewrite,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)
from tests.support.session_navigation_queue import create_catalog_intent


@pytest.mark.parametrize("column,value", [
    ("fencing_token", 1.5),
    ("fencing_token", -1),
    ("receipt_revision", 1.5),
    ("receipt_revision", 0),
    ("result_node_revision", 1.5),
    ("result_node_revision", 0),
    ("pending_settlement", 2),
    ("pending_settlement", 1),
    ("created_at", "invalid-time"),
    ("updated_at", "2026-10-23T00:00:00"),
    ("holder_id", ""),
    ("reserved_node_id", ""),
])
async def test_cold_start_rejects_corrupt_operation_metadata_before_recovery(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, column: str, value: object,
) -> None:
    container = startup_container
    record = await _committed_intent(container)
    path, files = _close_and_files(container)
    _rewrite(path, record.operation_id, {column: value})
    before, raw = _catalog_dump(path), path.read_bytes()
    with pytest.raises(CatalogMaintenanceRequiredError, match="navigation intent 原始记录损坏"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before and path.read_bytes() == raw
    _assert_body_unchanged(container, files)


@pytest.mark.parametrize("column,value", [("holder_id", None), ("fencing_token", 0)])
async def test_cold_start_rejects_running_record_without_claim_identity(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, column: str, value: object,
) -> None:
    container = startup_container
    catalog = container.session_catalog_service
    barrier = asyncio.Event()

    async def pause_drain() -> list[object]:
        await barrier.wait()
        return []

    # 只暂停调度，用真实受理与claim生成running，再破坏一个认领字段。
    with monkeypatch.context() as scheduling:
        scheduling.setattr(catalog.operations, "drain_once", pause_drain)
        await catalog.operations.start()
        try:
            intent = create_catalog_intent(catalog, "create_folder", name="待恢复认领")
            result = await catalog.operations.enqueue(
                NavigationMutationEnqueueRequest(intents=[intent]), local_navigation_scope(catalog.workspace_id),
            )
            assert result.receipts[0].state == "queued"
        finally:
            await catalog.operations.stop()
    queue = catalog.operations._queue
    async with catalog.operations._executor.queue_owner() as owner:
        with queue._store.write_transaction() as connection:
            head = queue.next_runnable(connection, catalog.workspace_id)
            assert head is not None and head.operation_id == intent.client_operation_id
            record = queue.claim_running(
                connection, record=head, holder_id=owner.holder_id,
                owner_id=owner.owner_id, owner_generation=owner.generation, now=utc_now_text(),
            )
            assert record is not None and record.state == "running"
    path, files = _close_and_files(container)
    _rewrite(path, record.operation_id, {column: value})
    before, raw = _catalog_dump(path), path.read_bytes()
    with pytest.raises(CatalogMaintenanceRequiredError, match="running缺少holder或fencing token"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before and path.read_bytes() == raw
    _assert_body_unchanged(container, files)


@pytest.mark.parametrize("column,value", [
    ("queue_seq", 1.5),
    ("fencing_token", 1.5),
    ("pending_settlement", 2),
])
async def test_live_read_and_replay_reject_metadata_without_rewriting_it(
    startup_container: AppContainer, column: str, value: object,
) -> None:
    container = startup_container
    catalog = container.session_catalog_service
    await catalog.operations.start()
    try:
        intent = create_catalog_intent(catalog, "create_folder", name="原回执元数据")
        scope = local_navigation_scope(catalog.workspace_id)
        await catalog.operations.enqueue(NavigationMutationEnqueueRequest(intents=[intent]), scope)
        record = await catalog.operations.await_terminal(intent.client_operation_id, scope)
        assert record.state == "committed"
        path = catalog.path_resolver.catalog_store.database_path
        _rewrite(path, record.operation_id, {column: value})
        before = _catalog_dump(path)
        with pytest.raises(ValueError, match="navigation operation"):
            catalog.operations.record(record.operation_id, scope)
        with pytest.raises(ValueError, match="navigation operation"):
            await catalog.operations.enqueue(NavigationMutationEnqueueRequest(intents=[intent]), scope)
        assert _catalog_dump(path) == before
    finally:
        await catalog.operations.stop()
