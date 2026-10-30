"""真实创建产出未发布child后，经导航删除接管原目录和控制库记录。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
)
from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.session_control_store import SessionControlStore, ThreadCreationRecord
from app.core.session_lifecycle_gate import OperationHolderGate
from app.core.thread_creation import ThreadCreationService
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
from tests.unit.core.session_deletion.test_operation_replay_gc import _entered, _turns
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)
from tests.unit.core.session_deletion.test_thread_creation_io import CreationWait
from tests.unit.core.session_deletion.test_thread_creation_io import (
    creation_wait as creation_wait,  # noqa: PLC0414
)


@pytest.mark.parametrize("stage", ["verify", "abort"])
@pytest.mark.parametrize("stop_worker", [False, True])
async def test_navigation_delete_settles_unpublished_child_io_and_resumes_original_record(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    creation_wait: Callable[[Path, ThreadCreationRecord, str], CreationWait],
    monkeypatch: pytest.MonkeyPatch, stage: str, stop_worker: bool,
) -> None:
    container, client, _ = navigation_api
    resolver = container.session_catalog_service.path_resolver
    operations = container.session_catalog_service.operations
    session = await container.session_service.create(SessionCreateRequest(title="未发布child的原属主"))
    peer = await container.session_service.create(SessionCreateRequest(title="无关导航节点"))
    source = resolver.resolve_session_node(session.session_id)
    manifest = (source / "session.json").read_bytes()
    child_key = "product-pending-child-io"
    interruption = RuntimeError("产品创建在rename前中断")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise interruption

    with container.thread_initial_execution_service._factory.for_owner_session(session.session_id) as service:
        with monkeypatch.context() as fault:
            fault.setattr(ThreadCreationService, "_rename_staging_to_final", interrupt)
            with pytest.raises(RuntimeError) as caught:
                await service.create(
                    idempotency_key=child_key, session_id=session.session_id,
                    thread_id=None, delegation_id=None, initial_state="idle",
                    session_metadata={
                        "graph_binding": asdict(DEEP_AGENT_GRAPH_BINDING),
                        "capability_profile": dict(DEEP_AGENT_CAPABILITY_PROFILE),
                        "task_seed": None, "task_reference": None,
                    }, artifact_manifest={"notes/plan.md": "保留原创建身份"},
                )
        assert caught.value is interruption
        child = service.control_store.get_thread_creation_record(child_key)
        assert child.state == "preparing" and not service.control_store.list_child_thread_rows()
    staging = source / child.staging_locator
    assert (staging / "thread.json").is_file()
    probe = creation_wait(source, child, stage)
    operation_id = create_prefixed_id("op")
    target = resolver.sessions_root / ".deleting" / operation_id / session.session_id
    loop_thread = threading.get_ident()
    worker = operations._worker_task
    assert worker is not None and not worker.done()
    stopping: asyncio.Task[None] | None = None
    holder_waiter: asyncio.Task[None] | None = None
    holder_entered = asyncio.Event()
    holders = OperationHolderGate(resolver.sessions_root, namespace="thread_creation")

    async def same_holder() -> None:
        async with holders.lock_for(f"{session.session_id}/{child_key}"):
            holder_entered.set()

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
        journal = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert journal.state == "deleting" and journal.drained_session_ids == ()
        assert (source / "session.json").read_bytes() == manifest and not target.exists()
        if stop_worker:
            stopping = asyncio.create_task(operations.stop())
        holder_waiter = asyncio.create_task(same_holder())
        await _turns()
        assert not worker.done() and (stopping is None or not stopping.done())
        assert not holder_entered.is_set() and resolver._delete_service._key_lock(operation_id).locked()
        response = await client.get(
            "/api/v1/session-catalog/roots", headers={"X-Request-ID": "child-cleanup-io-probe"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == "child-cleanup-io-probe"
        assert response.json()["request_id"] == "child-cleanup-io-probe"
        page = SessionCatalogPageDTO.model_validate(response.json()["data"])
        node_ids = {item.node_id for item in page.items}
        assert peer.session_id in node_ids and session.session_id not in node_ids
        with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == ("active", 1)
            assert control.get_thread_creation_record(child_key) == child
        probe.release()
        if stopping is not None:
            await stopping
            assert worker.cancelled() and operations.worker_state == "stopped"
            assert resolver.catalog_store.get_subtree_delete_record(operation_id) == journal
            assert (source / "session.json").read_bytes() == manifest and not target.exists()
            assert not staging.exists()
            with closing(SessionControlStore(source / "session-control.sqlite", read_only=True)) as control:
                assert control.get_fence() == ("active", 1)
                assert control.get_thread_creation_record(child_key).state == ("aborted" if stage == "abort" else "preparing")
            await operations.start()
        settled = await _settlement(client, operation_id, "settled")
        assert settled.state == "committed" and settled.receipt_revision > pending.receipt_revision
        await holder_waiter
        assert holder_entered.is_set() and len(probe.step.calls) == 1
        assert probe.step.calls[0] != loop_thread and probe.removals == [staging]
        assert not source.exists() and (target / "session.json").read_bytes() == manifest
        completed = resolver.catalog_store.get_subtree_delete_record(operation_id)
        assert completed.state == "completed" and completed.drained_session_ids == (session.session_id,)
        assert completed.frozen_session_locators == journal.frozen_session_locators
        with closing(SessionControlStore(target / "session-control.sqlite", read_only=True)) as control:
            aborted = control.get_thread_creation_record(child_key)
            assert aborted.state == "aborted" and aborted.abort_reason == "session_deletion_pending"
            assert (aborted.child_thread_id, aborted.artifact_manifest, aborted.preimage_hash) == (child.child_thread_id, child.artifact_manifest, child.preimage_hash)
            assert not control.list_child_thread_rows() and not control.list_initial_execution_intents()
    finally:
        probe.release()
        await asyncio.gather(*[task for task in (stopping, holder_waiter) if task is not None], return_exceptions=True)
