"""产品创建并删除节点后，物理回收等待不阻断导航HTTP，也不提前释放原gate。"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

import app.core.session_subtree_delete as deletion
from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.session_navigation import SessionCatalogPageDTO
from tests.integration.backend.sessions.navigation.test_delete_settlement_errors import (
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


@pytest.mark.parametrize("stage", ["remove", "barrier"])
async def test_product_deleted_node_gc_keeps_loop_and_original_gate(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    container, client, _ = navigation_api
    catalog = container.session_catalog_service
    resolver = catalog.path_resolver
    session = await container.session_service.create(SessionCreateRequest(title="真实回收节点"))
    peer = await container.session_service.create(SessionCreateRequest(title="保留导航节点"))
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
    record = resolver.catalog_store.get_subtree_delete_record(operation_id)
    target = resolver.sessions_root / ".deleting" / operation_id / session.session_id
    assert record.state == "completed" and not source.exists()
    assert (target / "session.json").read_bytes() == manifest
    now = datetime.fromisoformat(record.record_updated_at) + timedelta(days=2)
    original_remove, original_barrier = deletion.shutil.rmtree, deletion._fsync_directory
    loop_thread = threading.get_ident()

    def remove(path: Path) -> None:
        if path == target and stage == "remove":
            native_step.block()
        original_remove(path)

    def barrier(path: Path) -> None:
        if path == target.parent and stage == "barrier":
            native_step.block()
        original_barrier(path)

    monkeypatch.setattr(deletion.shutil, "rmtree", remove)
    monkeypatch.setattr(deletion, "_fsync_directory", barrier)
    collecting = asyncio.create_task(resolver.collect_expired_operation_replays(now=now))
    gate_entered = asyncio.Event()

    async def same_gate() -> None:
        async with resolver._delete_service._session_gate.exclusive(session.session_id):
            gate_entered.set()

    waiter: asyncio.Task[None] | None = None
    try:
        await _entered(native_step)
        collecting.cancel("取消真实回收等待")
        waiter = asyncio.create_task(same_gate())
        await _turns()
        assert not collecting.done() and not gate_entered.is_set()
        response = await client.get(
            "/api/v1/session-catalog/roots", headers={"X-Request-ID": "replay-gc-probe"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == "replay-gc-probe"
        assert response.json()["request_id"] == "replay-gc-probe"
        page = SessionCatalogPageDTO.model_validate(response.json()["data"])
        node_ids = {item.node_id for item in page.items}
        assert peer.session_id in node_ids and session.session_id not in node_ids
        assert resolver.catalog_store.get_subtree_delete_record(operation_id) == record
        if stage == "remove":
            assert (target / "session.json").read_bytes() == manifest
        native_step.release.set()
        with pytest.raises(asyncio.CancelledError, match="取消真实回收等待"):
            await collecting
        await waiter
        assert gate_entered.is_set() and not target.exists()
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        assert await resolver.collect_expired_operation_replays(now=now) is None
        assert len(native_step.calls) == 1
        assert resolver.catalog_store.get_subtree_delete_record(operation_id) == record
    finally:
        native_step.release.set()
        await asyncio.gather(collecting, *([waiter] if waiter is not None else []), return_exceptions=True)
