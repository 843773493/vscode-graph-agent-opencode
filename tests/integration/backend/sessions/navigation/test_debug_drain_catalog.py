"""真实删除在 Node 调试 catalog 读取期间保留原 gate、连接与恢复依据。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path

import httpx
import pytest

from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.session_control_store import SessionControlStore
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.node_debug.service import NodeDebugService
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


@pytest.fixture
def debug_catalog_wait(
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str], tuple[NativeStep, list[SessionControlStore], list[SessionControlStore]]]:
    in_drain: ContextVar[bool] = ContextVar("test_node_debug_drain", default=False)
    opened: list[SessionControlStore] = []
    closed: list[SessionControlStore] = []
    drain, initialize, close = NodeDebugService.drain_session, SessionControlStore.__init__, SessionControlStore.close
    children = SessionControlStore.list_child_thread_rows
    stage = ""

    async def observed_drain(service: NodeDebugService, session_id: str) -> None:
        token = in_drain.set(True)
        try:
            await drain(service, session_id)
        finally:
            in_drain.reset(token)

    def observed_open(control: SessionControlStore, database_path: Path, *, read_only: bool = False) -> None:
        initialize(control, database_path, read_only=read_only)
        if in_drain.get():
            opened.append(control)

    def observed_children(control: SessionControlStore):
        if in_drain.get() and stage == "query" and not native_step.calls:
            native_step.block()
        return children(control)

    def observed_close(control: SessionControlStore) -> None:
        tracked = control in opened and control not in closed
        if tracked and stage == "close" and not native_step.calls:
            native_step.block()
        close(control)
        if tracked:
            closed.append(control)

    monkeypatch.setattr(NodeDebugService, "drain_session", observed_drain)
    monkeypatch.setattr(SessionControlStore, "__init__", observed_open)
    monkeypatch.setattr(SessionControlStore, "list_child_thread_rows", observed_children)
    monkeypatch.setattr(SessionControlStore, "close", observed_close)

    def install(value: str):
        nonlocal stage
        stage = value
        return native_step, opened, closed

    return install


@pytest.mark.parametrize("stage", ["query", "close"])
@pytest.mark.parametrize("stop_worker", [False, True])
async def test_debug_catalog_wait_keeps_delete_owned_until_original_read_settles(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    debug_catalog_wait: Callable[[str], tuple[NativeStep, list[SessionControlStore], list[SessionControlStore]]],
    stage: str, stop_worker: bool,
) -> None:
    container, client, _ = navigation_api
    resolver = container.session_catalog_service.path_resolver
    operations = container.session_catalog_service.operations
    session = await container.session_service.create(SessionCreateRequest(title="调试目录排空"))
    peer = await container.session_service.create(SessionCreateRequest(title="无关会话仍可访问"))
    source = resolver.resolve_session_node(session.session_id)
    manifest = (source / "session.json").read_bytes()
    operation_id = create_prefixed_id("op")
    target = resolver.sessions_root / ".deleting" / operation_id / session.session_id
    step, opened, closed = debug_catalog_wait(stage)
    loop_thread = threading.get_ident()
    worker = operations._worker_task
    assert worker is not None and not worker.done()
    stopping: asyncio.Task[None] | None = None
    try:
        accepted = await client.post("/api/v1/session-catalog/operations:enqueue", json={
            "intents": [{"client_operation_id": operation_id, "client_sequence": 1, "kind": "delete_session",
                         "target_node_id": session.session_id, "base_catalog_revision": resolver.revision}],
        })
        assert accepted.status_code == 202, accepted.text
        await _entered(step)
        assert len(step.calls) == 1 and step.calls[0] != loop_thread
        pending = await _receipt(client, operation_id)
        record = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert pending.state == "committed" and pending.settlement == "pending"
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert len(opened) == 1 and not closed
        assert (source / "session.json").read_bytes() == manifest and not target.exists()
        with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == ("active", 1)
        if stop_worker:
            stopping = asyncio.create_task(operations.stop())
        await _turns()
        assert not worker.done() and (stopping is None or not stopping.done())
        assert resolver._delete_service._key_lock(operation_id).locked()
        async with resolver._delete_service._session_gate.exclusive(peer.session_id):
            assert resolver.catalog_store.get_node(peer.session_id).state == "active"
        response = await client.get("/api/v1/session-catalog/roots", headers={"X-Request-ID": "debug-catalog-probe"})
        assert response.status_code == 200, response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "debug-catalog-probe"
        assert resolver.catalog_store.get_subtree_delete_record(operation_id) == record
        step.release.set()
        if stopping is not None:
            await stopping
            assert worker.cancelled() and operations.worker_state == "stopped"
            assert resolver.catalog_store.get_subtree_delete_record(operation_id) == record
            assert (source / "session.json").read_bytes() == manifest and not target.exists()
            assert closed == opened
            with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
                assert control.get_fence() == ("active", 1)
            with pytest.raises(RuntimeError, match="已关闭"):
                _ = opened[0].connection
            await operations.start()
        settled = await _settlement(client, operation_id, "settled")
        assert settled.state == "committed" and settled.receipt_revision > pending.receipt_revision
        assert closed == opened and closed.count(opened[0]) == 1
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
        completed = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert completed.state == "completed" and completed.drained_session_ids == (session.session_id,)
        assert completed.frozen_session_locators == record.frozen_session_locators
        assert len(step.calls) == 1
    finally:
        step.release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
