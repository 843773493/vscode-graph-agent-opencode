"""产品删除收敛控制库等待期间，导航可用且原连接随worker取消结算。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.session_control_store import SessionControlStore
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.session_navigation import SessionCatalogPageDTO
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
from tests.unit.core.session_deletion.test_drain_control_io import ControlWait
from tests.unit.core.session_deletion.test_drain_control_io import (
    control_wait as control_wait,  # noqa: PLC0414
)
from tests.unit.core.session_deletion.test_operation_replay_gc import _entered, _turns
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)


@pytest.mark.parametrize("stage", ["open", "close"])
@pytest.mark.parametrize("stop_worker", [False, True])
async def test_navigation_control_wait_settles_original_connection_before_recovery(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    control_wait: Callable[[Path, str], ControlWait], stage: str, stop_worker: bool,
) -> None:
    container, client, _ = navigation_api
    resolver = container.session_catalog_service.path_resolver
    operations = container.session_catalog_service.operations
    session = await container.session_service.create(SessionCreateRequest(title="控制库原生等待"))
    peer = await container.session_service.create(SessionCreateRequest(title="无关导航节点"))
    source = resolver.resolve_session_node(session.session_id)
    manifest = (source / "session.json").read_bytes()
    operation_id = create_prefixed_id("op")
    target = resolver.sessions_root / ".deleting" / operation_id / session.session_id
    probe = control_wait(source / "session-control.sqlite", stage)
    loop_thread = threading.get_ident()
    worker = operations._worker_task
    assert worker is not None and not worker.done()
    stopping: asyncio.Task[None] | None = None
    try:
        accepted = await client.post("/api/v1/session-catalog/operations:enqueue", json={
            "intents": [{
                "client_operation_id": operation_id, "client_sequence": 1,
                "kind": "delete_session", "target_node_id": session.session_id,
                "base_catalog_revision": resolver.revision,
            }],
        })
        assert accepted.status_code == 202, accepted.text
        await _entered(probe.step)
        pending = await _receipt(client, operation_id)
        assert pending.state == "committed" and pending.settlement == "pending"
        record = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert set(record.frozen_session_locators) == {session.session_id}
        assert (source / "session.json").read_bytes() == manifest and not target.exists()
        with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == ("active", 1)
        if stage == "close":
            assert len(probe.opened) == 1 and not probe.closed
        if stop_worker:
            stopping = asyncio.create_task(operations.stop())
        await _turns()
        assert not worker.done() and (stopping is None or not stopping.done())
        assert resolver._delete_service._key_lock(operation_id).locked()
        async with resolver._delete_service._session_gate.exclusive(peer.session_id):
            assert resolver.catalog_store.get_node(peer.session_id).state == "active"
        response = await client.get(
            "/api/v1/session-catalog/roots", headers={"X-Request-ID": "control-io-probe"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == "control-io-probe"
        assert response.json()["request_id"] == "control-io-probe"
        page = SessionCatalogPageDTO.model_validate(response.json()["data"])
        node_ids = {item.node_id for item in page.items}
        assert peer.session_id in node_ids and session.session_id not in node_ids
        assert resolver.catalog_store.get_subtree_delete_record(operation_id) == record
        probe.release()
        if stopping is not None:
            await stopping
            assert worker.cancelled() and operations.worker_state == "stopped"
            assert resolver.catalog_store.get_subtree_delete_record(operation_id) == record
            assert (source / "session.json").read_bytes() == manifest and not target.exists()
            with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
                assert control.get_fence() == ("active", 1)
            first = probe.opened[0]
            assert probe.closed.count(first) == 1
            with pytest.raises(RuntimeError, match="已关闭"):
                _ = first.connection
            await operations.start()
        settled = await _settlement(client, operation_id, "settled")
        assert settled.state == "committed" and settled.receipt_revision > pending.receipt_revision
        first = probe.opened[0]
        assert probe.closed.count(first) == 1
        with pytest.raises(RuntimeError, match="已关闭"):
            _ = first.connection
        assert len(probe.step.calls) == 1 and probe.step.calls[0] != loop_thread
        assert all(thread_id != loop_thread for thread_id in probe.close_threads)
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
        completed = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert completed.state == "completed" and completed.drained_session_ids == (session.session_id,)
        assert completed.frozen_session_locators == record.frozen_session_locators
    finally:
        probe.release()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
