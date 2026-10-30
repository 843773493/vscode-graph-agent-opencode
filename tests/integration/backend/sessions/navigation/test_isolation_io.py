"""真实导航删除的原生隔离等待、worker关闭及同journal恢复。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import httpx
import pytest

import app.core.session_subtree_delete as deletion
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
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)


@pytest.mark.parametrize("stage", ["rename", "source-barrier", "fence-sqlite"])
@pytest.mark.parametrize("stop_worker", [False, True])
async def test_navigation_isolation_wait_preserves_original_owner_and_recovery(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch, stage: str,
    stop_worker: bool,
) -> None:
    container, client, _ = navigation_api
    catalog = container.session_catalog_service
    resolver, operations = catalog.path_resolver, catalog.operations
    session = await container.session_service.create(SessionCreateRequest(title="原生隔离等待"))
    peer = await container.session_service.create(SessionCreateRequest(title="无关导航节点"))
    source = resolver.resolve_session_node(session.session_id)
    manifest = (source / "session.json").read_bytes()
    operation_id = create_prefixed_id("op")
    target = resolver.sessions_root / ".deleting" / operation_id / session.session_id
    original_rename, original_barrier = deletion.os.rename, deletion._fsync_directory
    original_cas = SessionControlStore.cas_fence_transition
    blockers: list[sqlite3.Connection] = []
    renames: list[tuple[Path, Path]] = []
    loop_thread = threading.get_ident()

    def rename(old: Path, new: Path) -> None:
        if old == source and new == target:
            renames.append((old, new))
            if stage == "rename":
                native_step.block()
        original_rename(old, new)

    def barrier(path: Path) -> None:
        if stage == "source-barrier" and path == source.parent and not native_step.calls:
            native_step.block()
        original_barrier(path)

    def cas(control: SessionControlStore, expected_generation: int, new_state: str) -> bool:
        if stage == "fence-sqlite" and control.database_path == source / "session-control.sqlite" and not native_step.calls:
            blocker = sqlite3.connect(control.database_path, isolation_level=None, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            blockers.append(blocker)
            native_step.calls.append(threading.get_ident())
            native_step.entered.set()
        return original_cas(control, expected_generation, new_state)

    monkeypatch.setattr(deletion.os, "rename", rename)
    monkeypatch.setattr(deletion, "_fsync_directory", barrier)
    monkeypatch.setattr(SessionControlStore, "cas_fence_transition", cas)
    worker = operations._worker_task
    assert worker is not None and not worker.done()
    stopping: asyncio.Task[None] | None = None
    gate_waiter: asyncio.Task[None] | None = None
    gate_entered = asyncio.Event()

    async def same_gate() -> None:
        async with resolver._delete_service._session_gate.exclusive(session.session_id):
            gate_entered.set()

    try:
        accepted = await client.post("/api/v1/session-catalog/operations:enqueue", json={
            "intents": [{
                "client_operation_id": operation_id, "client_sequence": 1,
                "kind": "delete_session", "target_node_id": session.session_id,
                "base_catalog_revision": resolver.revision,
            }],
        })
        assert accepted.status_code == 202, accepted.text
        await _entered(native_step)
        pending = await _receipt(client, operation_id)
        assert pending.state == "committed" and pending.settlement == "pending"
        if stop_worker:
            stopping = asyncio.create_task(operations.stop())
        gate_waiter = asyncio.create_task(same_gate())
        await _turns()
        assert not worker.done() and not gate_entered.is_set()
        assert stopping is None or not stopping.done()
        response = await client.get(
            "/api/v1/session-catalog/roots", headers={"X-Request-ID": "isolation-io-probe"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == "isolation-io-probe"
        assert response.json()["request_id"] == "isolation-io-probe"
        page = SessionCatalogPageDTO.model_validate(response.json()["data"])
        node_ids = {item.node_id for item in page.items}
        assert peer.session_id in node_ids and session.session_id not in node_ids
        record = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert set(record.frozen_session_locators) == {session.session_id}
        physical = source if stage in ("rename", "fence-sqlite") else target
        assert (physical / "session.json").read_bytes() == manifest
        with closing(SessionControlStore(physical / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == (("active", 1) if stage == "fence-sqlite" else ("deleting", 2))
        native_step.release.set()
        for blocker in blockers:
            blocker.execute("ROLLBACK")
        if stopping is not None:
            await stopping
            assert worker.cancelled() and operations.worker_state == "stopped"
            assert resolver.catalog_store.get_subtree_delete_record(operation_id) == record
            stopped_physical = source if stage == "fence-sqlite" else target
            assert source.exists() == (stage == "fence-sqlite")
            assert (stopped_physical / "session.json").read_bytes() == manifest
            await operations.start()
        settled = await _settlement(client, operation_id, "settled")
        assert settled.state == "committed" and settled.receipt_revision > pending.receipt_revision
        await gate_waiter
        assert gate_entered.is_set() and renames == [(source, target)]
        assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
        completed = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert completed.state == "completed" and completed.drained_session_ids == (session.session_id,)
        assert completed.frozen_session_locators == record.frozen_session_locators
    finally:
        native_step.release.set()
        for blocker in blockers:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")
            blocker.close()
        await asyncio.gather(*[task for task in (stopping, gate_waiter) if task is not None], return_exceptions=True)
