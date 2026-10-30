"""真实执行历史的child fork在准入或目录I/O取消后沿原冻结创建继续发布。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable

import pytest

from app.container import AppContainer
from app.core import thread_creation
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import OperationHolderGate, SessionLifecycleGate
from app.schemas.internal_v2.session import SessionDTO
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _main_thread_node,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)


@pytest.mark.parametrize("stage", ["staging", "rename", "published"])
async def test_product_child_fork_keeps_original_creation_until_directory_io_settles(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = fork_execution_source
    request = await _fork_request(context, source.session_id, creation_kind="new_child_thread")
    key = context.forks._creation_key(request.idempotency_key)
    resolver = context.sessions.path_resolver
    canonical = _main_thread_node(context, source.session_id) / "rollout/rollout.jsonl"
    source_bytes = canonical.read_bytes()
    loop_thread = threading.get_ident()
    original_verify = thread_creation.verify_node_directory
    original = getattr(thread_creation.ThreadCreationService, "_prepare_staging" if stage == "staging" else "_rename_staging_to_final")
    holders = OperationHolderGate(resolver.sessions_root, namespace="thread_creation")
    holder_entered = asyncio.Event()

    def wait(self, *args):
        if not native_step.calls:
            native_step.block()
        return original(self, *args)

    def verify(*args, **kwargs):
        if stage == "published" and kwargs.get("stage") == "published 复验" and not native_step.calls:
            native_step.block()
        return original_verify(*args, **kwargs)

    async def same_holder() -> None:
        async with holders.lock_for(f"{source.session_id}/{key}"):
            holder_entered.set()

    with monkeypatch.context() as patch:
        if stage == "published":
            patch.setattr(thread_creation, "verify_node_directory", verify)
        else:
            patch.setattr(thread_creation.ThreadCreationService, "_prepare_staging" if stage == "staging" else "_rename_staging_to_final", wait)
        creating = asyncio.create_task(context.forks.fork(request))
        holder = None
        try:
            await _entered(native_step)
            with context.forks._threads.for_owner_session(source.session_id) as service:
                control = service.control_store
                frozen = control.get_thread_creation_record(key)
                assert frozen.state == ("published" if stage == "published" else "preparing")
                assert frozen.purpose == "fork" and frozen.initial_state == "idle"
                assert not control.list_initial_execution_intents()
            holder = asyncio.create_task(same_holder())
            creating.cancel("取消原fork目录等待")
            await _turns()
            assert not creating.done() and not holder_entered.is_set()
            assert canonical.read_bytes() == source_bytes
            assert (await context.sessions.get(source.session_id)).session_id == source.session_id
            native_step.release.set()
            with pytest.raises(asyncio.CancelledError, match="取消原fork目录等待"):
                await creating
            await holder
            assert holder_entered.is_set() and native_step.calls[0] != loop_thread
            with context.forks._threads.for_owner_session(source.session_id) as service:
                assert service.control_store.get_thread_creation_record(key) == frozen
            result = await context.forks.fork(request)
            assert result.thread_id == frozen.child_thread_id
            assert await context.forks.fork(request) == result
            with context.forks._threads.for_owner_session(source.session_id) as service:
                published = service.control_store.get_thread_creation_record(key)
                assert (published.child_thread_id, published.final_relative_locator, published.preimage_hash) == (
                    frozen.child_thread_id, frozen.final_relative_locator, frozen.preimage_hash,
                )
                assert published.state == "published" and published.abort_reason is None
                assert len(service.control_store.list_child_thread_rows()) == 1
                assert not service.control_store.list_initial_execution_intents()
            target = resolver.resolve_thread_node(source.session_id, result.thread_id)
            assert (target / "rollout/rollout.jsonl").read_bytes() == source_bytes
            assert canonical.read_bytes() == source_bytes
            assert len(native_step.calls) == 1
        finally:
            native_step.release.set()
            pending: list[Awaitable[object]] = [task for task in (creating, holder) if task is not None]
            await asyncio.gather(*pending, return_exceptions=True)
            # 失败也沿产品入口收尾原fork与原60秒watchdog，不能留下未结算操作。
            await context.forks.fork(request)


@pytest.mark.parametrize("stage", ["record", "manifest"])
async def test_product_child_fork_settles_admission_before_cancel_releases_gates(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = fork_execution_source
    request = await _fork_request(context, source.session_id, creation_kind="new_child_thread")
    key = context.forks._creation_key(request.idempotency_key)
    resolver = context.sessions.path_resolver
    canonical = _main_thread_node(context, source.session_id) / "rollout/rollout.jsonl"
    source_bytes = canonical.read_bytes()
    loop_thread = threading.get_ident()
    method = "create_or_get_thread_creation_record" if stage == "record" else "freeze_thread_creation_artifact_manifest"
    original = getattr(SessionControlStore, method)
    blockers: list[sqlite3.Connection] = []

    def wait(control, *args, **kwargs):
        if stage == "record" and not native_step.calls:
            blocker = sqlite3.connect(control.database_path, isolation_level=None, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            blockers.append(blocker)
            native_step.calls.append(threading.get_ident())
            native_step.entered.set()
        result = original(control, *args, **kwargs)
        if stage == "manifest" and not native_step.calls:
            native_step.block()
        return result

    def release() -> None:
        native_step.release.set()
        for blocker in blockers:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")

    holders = OperationHolderGate(resolver.sessions_root, namespace="thread_creation")
    session_gate = SessionLifecycleGate(resolver.sessions_root)
    gates = [holders.lock_for(f"{source.session_id}/{key}"), session_gate.exclusive(source.session_id)]
    entered = [asyncio.Event() for _ in gates]

    async def contend(index: int) -> None:
        async with gates[index]:
            entered[index].set()

    with monkeypatch.context() as patch:
        patch.setattr(SessionControlStore, method, wait)
        creating = asyncio.create_task(context.forks.fork(request))
        tasks: list[Awaitable[object]] = [creating]
        try:
            await _entered(native_step)
            tasks.extend(asyncio.create_task(contend(index)) for index in range(len(gates)))
            creating.cancel("取消原fork准入等待")
            await _turns()
            assert not creating.done() and not any(event.is_set() for event in entered)
            assert canonical.read_bytes() == source_bytes
            release()
            with pytest.raises(asyncio.CancelledError, match="取消原fork准入等待"):
                await creating
            await asyncio.gather(*tasks[1:])
            assert all(event.is_set() for event in entered)
            assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
            with context.forks._threads.for_owner_session(source.session_id) as service:
                frozen = service.control_store.get_thread_creation_record(key)
                assert frozen.state == "preparing" and frozen.artifact_manifest is not None
                assert not service.control_store.list_child_thread_rows()
                assert not service.control_store.list_initial_execution_intents()
            result = await context.forks.fork(request)
            assert result.thread_id == frozen.child_thread_id
            assert await context.forks.fork(request) == result
            with context.forks._threads.for_owner_session(source.session_id) as service:
                published = service.control_store.get_thread_creation_record(key)
                assert (published.child_thread_id, published.final_relative_locator, published.preimage_hash,
                        published.artifact_manifest) == (frozen.child_thread_id, frozen.final_relative_locator,
                                                       frozen.preimage_hash, frozen.artifact_manifest)
                assert published.state == "published" and published.abort_reason is None
                assert len(service.control_store.list_child_thread_rows()) == 1
                assert not service.control_store.list_initial_execution_intents()
            target = resolver.resolve_thread_node(source.session_id, result.thread_id)
            assert (target / "rollout/rollout.jsonl").read_bytes() == source_bytes
            assert canonical.read_bytes() == source_bytes and len(native_step.calls) == 1
        finally:
            release()
            await asyncio.gather(*tasks, return_exceptions=True)
            for blocker in blockers:
                blocker.close()
            await context.forks.fork(request)
