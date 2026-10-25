"""子树删除单次预算覆盖真实锁、阶段切换及原错误归属。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack, closing
from pathlib import Path

import pytest

import app.core.session_subtree_delete as deletion
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import OperationHolderGate
from app.core.session_subtree_delete import SessionSubtreeDeleteService
from tests.unit.core.test_session_subtree_delete import (
    create_physical_session,
    date_bucket_dir,
)
from tests.unit.core.test_session_subtree_delete import (
    service as service,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import (
    sessions_root as sessions_root,  # noqa: PLC0414
)
from tests.unit.core.test_session_subtree_delete import store as store  # noqa: PLC0414


@pytest.fixture
async def advance_delete_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = 0.0

    def advance(seconds: float) -> None:
        nonlocal offset
        offset += seconds

    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
    return advance


@pytest.mark.parametrize("stage", ["key", "topology", "retention", "holder", "session", "resource", "finish"])
async def test_delete_deadline_covers_each_wait_and_resumes_original_record(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, monkeypatch: pytest.MonkeyPatch,
    advance_delete_clock: Callable[[float], None], stage: str,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    source = date_bucket_dir(sessions_root, node.storage_relative_locator)
    key = "deadline-each-wait"
    isolated = sessions_root / ".deleting" / key / node.node_id
    reached, release = asyncio.Event(), asyncio.Event()
    drains: list[str] = []

    async with AsyncExitStack() as blockers:
        async def observe(operation_id: str, phase: str) -> None:
            assert operation_id == key
            if stage in ("holder", "session") and phase == f"deletion.drain_waiting/{node.node_id}":
                reached.set()
            if stage == "finish" and phase == f"deletion.renamed/{node.node_id}":
                await blockers.enter_async_context(service._gate.exclusive())
                reached.set()

        async def retention(session_id: str) -> None:
            assert session_id == node.node_id
            if stage == "retention":
                reached.set()
                await release.wait()

        async def drain(session_id: str) -> None:
            drains.append(session_id)
            if stage == "resource":
                reached.set()
                await release.wait()

        monkeypatch.setattr(deletion, "observe_phase", observe)
        service.set_fork_retention_release_callback(retention)
        holder = OperationHolderGate(sessions_root, namespace="delete_deadline")
        service.set_session_drain_callback(drain, holders=(holder,) if stage == "holder" else ())
        if stage == "key":
            await blockers.enter_async_context(service._key_lock(key))
        elif stage == "topology":
            await blockers.enter_async_context(service._gate.exclusive())
        elif stage == "holder":
            await blockers.enter_async_context(holder.lock_for(node.node_id))
        elif stage == "session":
            await blockers.enter_async_context(service._session_gate.shared(node.node_id))

        deleting = asyncio.create_task(service.delete(idempotency_key=key, root_node_id=node.node_id))
        try:
            if stage in ("key", "topology"):
                await asyncio.sleep(0)
            else:
                await asyncio.wait_for(reached.wait(), 1)
            assert not deleting.done()
            advance_delete_clock(31)
            expected_phase = {"key": "key-lock", "topology": "catalog-mark", "finish": "catalog-finish"}.get(stage, "drain")
            with pytest.raises(TimeoutError, match=f"operation_id={key}, phase={expected_phase}"):
                await asyncio.wait_for(asyncio.shield(deleting), 1)
            assert not service._key_lock(key).locked() or stage == "key"
            if stage in ("key", "topology"):
                with pytest.raises(KeyError):
                    store.get_subtree_delete_record(key)
                assert store.get_node(node.node_id).state == "active"
            else:
                record = store.get_subtree_delete_record(key)
                assert record.state == ("draining" if stage == "finish" else "deleting")
                assert record.drained_session_ids == ((node.node_id,) if stage == "finish" else ())
                assert record.frozen_session_locators == {node.node_id: node.storage_relative_locator}
                assert store.get_node(node.node_id).state == "deleting"
            physical = isolated if stage == "finish" else source
            assert physical.is_dir()
            assert isolated.exists() == (stage == "finish")
            with closing(SessionControlStore(physical / "session-control.sqlite")) as control:
                assert control.get_fence() == (("deleting", 2) if stage == "finish" else ("active", 1))
        finally:
            deleting.cancel()
            await asyncio.gather(deleting, return_exceptions=True)

    release.set()
    result = await service.delete(idempotency_key=key, root_node_id=node.node_id)
    assert result.record_state == "completed" and not source.exists() and isolated.is_dir()
    assert drains == [node.node_id] * (2 if stage == "resource" else 1)


async def test_delete_budget_is_shared_between_key_wait_and_resource_drain(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, advance_delete_clock: Callable[[float], None],
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    key = "deadline-shared"
    reached = asyncio.Event()

    async def drain(_session_id: str) -> None:
        reached.set()
        await asyncio.Event().wait()

    service.set_session_drain_callback(drain)
    async with service._key_lock(key):
        deleting = asyncio.create_task(service.delete(idempotency_key=key, root_node_id=node.node_id))
        await asyncio.sleep(0)
        advance_delete_clock(20)
        await asyncio.sleep(0)
        assert not deleting.done()
    try:
        await asyncio.wait_for(reached.wait(), 1)
        advance_delete_clock(11)
        with pytest.raises(TimeoutError, match="phase=drain"):
            await asyncio.wait_for(asyncio.shield(deleting), 1)
        record = store.get_subtree_delete_record(key)
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert date_bucket_dir(sessions_root, node.storage_relative_locator).is_dir()
    finally:
        deleting.cancel()
        await asyncio.gather(deleting, return_exceptions=True)


@pytest.mark.parametrize("error", [TimeoutError("底层原始超时"), asyncio.CancelledError("调用者取消")])
async def test_delete_preserves_underlying_timeout_and_cancellation_identity(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path, error: BaseException,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    key = "deadline-original-error"

    async def drain(_session_id: str) -> None:
        raise error

    service.set_session_drain_callback(drain)
    with pytest.raises(type(error)) as caught:
        await service.delete(idempotency_key=key, root_node_id=node.node_id)
    assert caught.value is error
    record = store.get_subtree_delete_record(key)
    assert record.state == "deleting" and record.drained_session_ids == ()
    assert date_bucket_dir(sessions_root, node.storage_relative_locator).is_dir()
    assert not service._key_lock(key).locked()


async def test_expired_inherited_deadline_cannot_mark_or_create_delete_record(
    service: SessionSubtreeDeleteService, store: SessionCatalogStore,
    sessions_root: Path,
) -> None:
    node = create_physical_session(store, sessions_root, parent_node_id=None)
    with pytest.raises(deletion.SubtreeDeleteWaitTimeout, match="phase=key-lock"):
        await service.delete(
            idempotency_key="expired-before-entry", root_node_id=node.node_id,
            deadline=asyncio.get_running_loop().time() - 1,
        )
    assert store.get_node(node.node_id).state == "active"
    assert date_bucket_dir(sessions_root, node.storage_relative_locator).is_dir()
    with pytest.raises(KeyError):
        store.get_subtree_delete_record("expired-before-entry")
