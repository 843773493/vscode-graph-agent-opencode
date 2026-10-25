"""真实消息执行后，runtime 排空超时不得隔离或删除 Session 的恢复依据。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest

import app.core.session_subtree_delete as deletion
from app.core.checkpoint_config import build_checkpoint_config
from app.core.resource_uri import parse_attachment_uri
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_attachment_runtime_lifecycle import (
    execute_upload,
    isolate_unused_external_managers,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import RuntimeCase
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)


@pytest.mark.parametrize("deadline_source", ["session", "runtime"])
async def test_delete_timeout_keeps_locator_control_history_and_original_unload(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
    deadline_source: str,
) -> None:
    case = runtime_case
    isolate_unused_external_managers(case, monkeypatch)
    sid, tid, _ref = await execute_upload(case)
    container = case.runner.container
    resolver = container.session_service.path_resolver
    node = resolver.resolve_session_node(sid)
    history = case.runner.saver._storage.storage.jsonl_path(sid, thread_id=tid)
    original_history = history.read_bytes()
    entered, finish = asyncio.Event(), asyncio.Event()
    attempts: list[tuple[str, str, int]] = []
    generation = case.tracker.snapshot(sid, tid).generation

    async def held_release(session: str, thread: str, current_generation: int) -> None:
        attempts.append((session, thread, current_generation))
        entered.set()
        await finish.wait()

    case.tracker.add_unload_listener(held_release)
    if deadline_source == "runtime":
        # 独立核对原 runtime 的30秒错误，避免被新的整次删除预算先截断。
        monkeypatch.setattr(deletion, "SUBTREE_DELETE_WAIT_SECONDS", 60)
    key = "runtime-drain-deadline"
    deleting = asyncio.create_task(resolver.delete_subtree(idempotency_key=key, root_node_id=sid))
    try:
        await entered.wait()
        state = case.tracker._states[(sid, tid)]
        original_unload = state.runtime_unload
        assert original_unload is not None
        loop = asyncio.get_running_loop()
        original_time = loop.time
        monkeypatch.setattr(loop, "time", lambda: original_time() + 30)
        for _ in range(12):
            await asyncio.sleep(0)
        assert deleting.done(), "真实 Session 删除应收到精确归属的等待超时"
        if deadline_source == "runtime":
            with pytest.raises(ExceptionGroup) as failure:
                await deleting
            assert any(isinstance(error, TimeoutError) and f"thread_id={tid}" in str(error)
                       for error in failure.value.exceptions)
        else:
            with pytest.raises(TimeoutError, match=f"root_node_id={sid}, operation_id={key}, phase=drain"):
                await deleting
        assert state.runtime_unload is original_unload and not original_unload.done()
        assert node.is_dir() and history.read_bytes() == original_history
        record = resolver.catalog_store.get_subtree_delete_record(key)
        assert record.state == "deleting" and record.drained_session_ids == ()
        assert resolver.catalog_store.get_node(sid).storage_relative_locator == record.frozen_session_locators[sid]
        assert not (container.workspace_root / ".boxteam/sessions/.deleting" / key / sid).exists()
        control = SessionControlStore(node / "session-control.sqlite")
        try:
            assert control.get_fence() == ("active", 1)
        finally:
            control.close()
        assert attempts == [(sid, tid, generation)]
        finish.set()
        await original_unload
        result = await resolver.delete_subtree(idempotency_key=key, root_node_id=sid)
        assert result.record_state == "completed" and not node.exists()
        assert attempts == [(sid, tid, generation)]
    finally:
        finish.set()
        await asyncio.gather(deleting, return_exceptions=True)


async def test_delete_uses_remaining_budget_after_key_wait_and_preserves_blocked_session(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    isolate_unused_external_managers(case, monkeypatch)
    sid, tid, ref = await execute_upload(case)
    container = case.runner.container
    resolver = container.session_service.path_resolver
    service = resolver._delete_service
    node = resolver.resolve_session_node(sid)
    history = case.runner.saver._storage.storage.jsonl_path(sid, thread_id=tid)
    original_bytes = history.read_bytes()
    generation = case.tracker.snapshot(sid, tid).generation
    key = "runtime-shared-delete-budget"
    reached = asyncio.Event()
    releases: list[tuple[str, str, int]] = []

    async def observe(operation_id: str, phase: str) -> None:
        if operation_id == key and phase == f"deletion.drain_waiting/{sid}":
            reached.set()

    async def listener(session: str, thread: str, current_generation: int) -> None:
        releases.append((session, thread, current_generation))

    monkeypatch.setattr(deletion, "observe_phase", observe)
    case.tracker.add_unload_listener(listener)
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = 0
    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
    async with AsyncExitStack() as holders:
        await holders.enter_async_context(service._session_gate.shared(sid))
        async with service._key_lock(key):
            deleting = asyncio.create_task(resolver.delete_subtree(idempotency_key=key, root_node_id=sid))
            await asyncio.sleep(0)
            offset = 20
            await asyncio.sleep(0)
            assert not deleting.done()
        try:
            await asyncio.wait_for(reached.wait(), 2)
            # 真实Session gate尚被旧读者持有，无关Session仍沿同一删除入口完成。
            peer = await container.session_service.create(SessionCreateRequest(title="独立删除对照"))
            peer_key = "runtime-shared-delete-budget-peer"
            assert service._key_lock(peer_key) is not service._key_lock(key)
            peer_result = await resolver.delete_subtree(idempotency_key=peer_key, root_node_id=peer.session_id)
            assert peer_result.record_state == "completed"
            offset = 31
            with pytest.raises(TimeoutError, match=f"operation_id={key}, phase=drain"):
                await asyncio.wait_for(asyncio.shield(deleting), 2)
            record = resolver.catalog_store.get_subtree_delete_record(key)
            assert record.state == "deleting" and record.drained_session_ids == ()
            assert resolver.catalog_store.get_node(sid).storage_relative_locator == record.frozen_session_locators[sid]
            assert history.read_bytes() == original_bytes and node.is_dir()
            assert not (container.workspace_root / ".boxteam/sessions/.deleting" / key / sid).exists()
            assert case.tracker.snapshot(sid, tid).generation == generation and releases == []
            directory = container.attachment_blob_store._catalog.get_directory(
                parse_attachment_uri(ref.file_id, require_file=True).attachment_id,
            )
            assert directory.unattached_since is None and directory.gc_after is None
            async with service._session_gate.shared(sid):
                # 超时调用已归还自身等待，仍由原读者保护完整恢复依据。
                control = SessionControlStore(node / "session-control.sqlite")
                try:
                    assert control.get_fence() == ("active", 1)
                finally:
                    control.close()
        finally:
            deleting.cancel()
            await asyncio.gather(deleting, return_exceptions=True)
    result = await resolver.delete_subtree(idempotency_key=key, root_node_id=sid)
    assert result.record_state == "completed" and not node.exists()
    assert releases == [(sid, tid, generation)]


@pytest.mark.parametrize("phase", ["listener", "scope"])
@pytest.mark.parametrize("fails_later", [False, True])
async def test_service_shutdown_drains_peer_runtime_while_original_cleanup_is_pending(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
    phase: str, fails_later: bool,
) -> None:
    case = runtime_case
    isolate_unused_external_managers(case, monkeypatch)
    owners = [await execute_upload(case), await execute_upload(case)]
    (first_session, first_thread, _first_ref), (peer_session, peer_thread, peer_ref) = owners
    container = case.runner.container
    scopes = {owner: scope for owner, _runtime, scope in case.compiled}
    first_scope, peer_scope = scopes[(first_session, first_thread)], scopes[(peer_session, peer_thread)]
    histories = {
        (session, thread): case.runner.saver._storage.storage.jsonl_path(session, thread_id=thread)
        for session, thread, _ref in owners
    }
    original_bytes = {owner: path.read_bytes() for owner, path in histories.items()}
    resolver = container.session_service.path_resolver
    locators = {session: resolver.resolve_session_node(session) for session, _thread, _ref in owners}
    generation = case.tracker.snapshot(first_session, first_thread).generation
    entered, finish, peer_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_failure = ConnectionError("原 runtime 清理的迟到失败")
    attempts = 0

    async def held_release() -> None:
        nonlocal attempts
        attempts += 1
        entered.set()
        await finish.wait()
        if fails_later and attempts == 1:
            raise original_failure

    async def listener(session: str, thread: str, current_generation: int) -> None:
        if (session, thread) == (first_session, first_thread):
            assert current_generation == generation
            await held_release()

    if phase == "listener":
        case.tracker.add_unload_listener(listener)
    else:
        first_scope.register(held_release, label="真实 runtime scope 的受控清理边界")
    peer_scope.register(peer_closed.set, label="真实 peer scope 关闭观察")
    closing = asyncio.create_task(container.agent_execution_service.shutdown())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(peer_closed.wait(), 5)
        # 原 scope 和 tracker 各自发布真实关闭，不从回调发生推断整个 owner 已完成。
        async with asyncio.timeout(5):
            while (peer_scope.state != "closed"
                   or case.tracker.snapshot(peer_session, peer_thread).residency != "cold"):
                await asyncio.sleep(0)
        assert case.tracker.snapshot(peer_session, peer_thread).residency == "cold"
        assert not closing.done() and first_scope.state != "closed"
        state = case.tracker._states[(first_session, first_thread)]
        original_unload = state.runtime_unload
        assert original_unload is not None and not original_unload.done()
        assert state.generation == generation
        peer_directory = container.attachment_blob_store._catalog.get_directory(
            parse_attachment_uri(peer_ref.file_id, require_file=True).attachment_id,
        )
        assert peer_directory.unattached_since is not None and peer_directory.gc_after is not None
        assert not any(
            (session, thread) == (peer_session, peer_thread)
            for session, thread, _token in container.attachment_blob_store._catalog.runtime_bindings()
        )
        for owner, path in histories.items():
            assert path.read_bytes() == original_bytes[owner]
            assert resolver.resolve_session_node(owner[0]) == locators[owner[0]]
            assert locators[owner[0]].is_dir()
            with session_thread_owner_scope(
                MutationIntentOwner(session_id=owner[0], thread_id=owner[1]), langgraph_thread_id=owner[1],
            ):
                assert await case.runner.saver.aget_tuple(build_checkpoint_config(owner[1])) is not None
        finish.set()
        if fails_later:
            with pytest.raises(ExceptionGroup) as failure:
                await closing
            thread_error = failure.value.exceptions[0].exceptions[0]
            if phase == "scope":
                assert thread_error.exceptions[0].__cause__ is original_failure
            else:
                assert thread_error is original_failure
            assert case.tracker._states[(first_session, first_thread)].generation == generation
            assert first_scope.state != "closed"
            await container.agent_execution_service.shutdown()
        else:
            await closing
        assert first_scope.state == "closed"
        assert attempts == (2 if fails_later else 1)
        assert not case.tracker._states
        for owner, path in histories.items():
            assert path.read_bytes() == original_bytes[owner]
    finally:
        finish.set()
        await asyncio.gather(closing, return_exceptions=True)
