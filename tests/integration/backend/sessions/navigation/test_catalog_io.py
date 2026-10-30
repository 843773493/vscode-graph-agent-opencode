"""产品删除的 catalog 等待与 worker 停止、原 journal 恢复使用同一事务链。"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta

import httpx
import pytest

from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.navigation.test_delete_settlement_errors import (
    _receipt,
    _settlement,
)
from tests.integration.backend.sessions.navigation.test_public_errors import (
    navigation_api as navigation_api,  # noqa: PLC0414
)
from tests.integration.backend.sessions.navigation.test_public_errors import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)


@pytest.mark.parametrize("stage", ["mark", "finish"])
async def test_product_delete_catalog_wait_keeps_owner_until_original_commit(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    container, client, _ = navigation_api
    catalog = container.session_catalog_service
    resolver, operations = catalog.path_resolver, catalog.operations
    store = resolver.catalog_store
    session = await container.session_service.create(SessionCreateRequest(title="原 catalog 删除事务"))
    source = resolver.resolve_session_node(session.session_id)
    manifest = (source / "session.json").read_bytes()
    operation_id = create_prefixed_id("op")
    method = "mark_subtree_deleting" if stage == "mark" else "finish_subtree_delete"
    original = getattr(store, method)
    target = resolver.sessions_root / ".deleting" / operation_id / session.session_id
    loop_thread = threading.get_ident()

    def wait(key, **kwargs):
        if key == operation_id and not native_step.calls:
            native_step.block()
        return original(key, **kwargs)

    monkeypatch.setattr(store, method, wait)
    accepted = await client.post("/api/v1/session-catalog/operations:enqueue", json={
        "intents": [{
            "client_operation_id": operation_id, "client_sequence": 1,
            "kind": "delete_session", "target_node_id": session.session_id,
            "base_catalog_revision": resolver.revision,
        }],
    })
    assert accepted.status_code == 202, accepted.text
    worker = operations._worker_task
    assert worker is not None
    stopping: asyncio.Task[None] | None = None
    try:
        await _entered(native_step)
        record = store.get_subtree_delete_record(operation_id)
        pending = await _receipt(client, operation_id)
        assert pending.state == ("running" if stage == "mark" else "committed")
        assert record.state == ("preparing" if stage == "mark" else "draining")
        physical = source if stage == "mark" else target
        assert (physical / "session.json").read_bytes() == manifest
        stopping = asyncio.create_task(operations.stop())
        await _turns()
        assert not stopping.done() and not worker.done()
        assert store.get_subtree_delete_record(operation_id) == record
        native_step.release.set()
        await stopping
        assert worker.cancelled() and operations.worker_state == "stopped"
        committed = store.get_subtree_delete_record(operation_id)
        assert committed.state == ("deleting" if stage == "mark" else "completed")
        assert committed.frozen_session_locators == record.frozen_session_locators
        assert (physical / "session.json").read_bytes() == manifest
        await operations.start()
        settled = await _settlement(client, operation_id, "settled")
        assert settled.state == "committed" and settled.receipt_revision > pending.receipt_revision
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
        completed = store.get_subtree_delete_record(operation_id)
        assert completed.state == "completed" and completed.drained_session_ids == (session.session_id,)
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
    finally:
        native_step.release.set()
        if stopping is not None:
            await stopping


async def test_product_gc_catalog_read_cancel_keeps_original_node_for_same_owner_retry(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, client, _ = navigation_api
    resolver = container.session_catalog_service.path_resolver
    session = await container.session_service.create(SessionCreateRequest(title="原回收清单"))
    source = resolver.resolve_session_node(session.session_id)
    manifest = (source / "session.json").read_bytes()
    operation_id = create_prefixed_id("op")
    accepted = await client.post("/api/v1/session-catalog/operations:enqueue", json={
        "intents": [{
            "client_operation_id": operation_id, "client_sequence": 1,
            "kind": "delete_session", "target_node_id": session.session_id,
            "base_catalog_revision": resolver.revision,
        }],
    })
    assert accepted.status_code == 202, accepted.text
    await _settlement(client, operation_id, "settled")
    store = resolver.catalog_store
    record = store.get_subtree_delete_record(operation_id)
    now = datetime.fromisoformat(record.record_updated_at) + timedelta(days=2)
    target = resolver.sessions_root / ".deleting" / operation_id / session.session_id
    original = store.list_completed_subtree_deletes
    loop_thread = threading.get_ident()

    def list_records(workspace_id):
        native_step.block()
        return original(workspace_id)

    monkeypatch.setattr(store, "list_completed_subtree_deletes", list_records)
    collecting = asyncio.create_task(resolver.collect_expired_operation_replays(now=now))
    try:
        await _entered(native_step)
        collecting.cancel("取消产品回收清单")
        await _turns()
        assert not collecting.done() and (target / "session.json").read_bytes() == manifest
        assert (await _receipt(client, operation_id)).settlement == "settled"
        native_step.release.set()
        with pytest.raises(asyncio.CancelledError, match="取消产品回收清单"):
            await collecting
        assert (target / "session.json").read_bytes() == manifest
        assert store.get_subtree_delete_record(operation_id) == record
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        monkeypatch.setattr(store, "list_completed_subtree_deletes", original)
        assert await resolver.collect_expired_operation_replays(now=now) is None
        assert not target.exists() and store.get_subtree_delete_record(operation_id) == record
    finally:
        native_step.release.set()
        await asyncio.gather(collecting, return_exceptions=True)
