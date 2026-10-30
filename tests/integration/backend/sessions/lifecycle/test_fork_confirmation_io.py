"""Fork 发布确认的原 worker 结算前不能释放目标 gate。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from app.container import AppContainer
from app.core.session_control_store import SessionControlStore
from app.core.session_control_store.copy_attachment_settlement import (
    read_copy_attachment_settlements,
)
from app.core.session_lifecycle_gate import NavigationTopologyGate, SessionLifecycleGate
from app.schemas.internal_v2.session import SessionDTO, SessionForkCreationKind
from app.services.infrastructure.rollout_context.checkpoint import (
    fork_owner as confirmation,
)
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


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("stage", ["before", "after", "pool"])
async def test_published_fork_confirmation_settles_before_cancellation_releases_gate(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch,
    creation_kind: SessionForkCreationKind, stage: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = fork_execution_source
    request = await _fork_request(context, source.session_id, creation_kind=creation_kind)
    key = context.forks._creation_key(request.idempotency_key)
    resolver = context.sessions.path_resolver
    catalog = resolver.catalog_store
    canonical = _main_thread_node(context, source.session_id) / "rollout/rollout.jsonl"
    source_bytes = canonical.read_bytes()
    attachments = production_fork_container.attachment_blob_store
    original_mark = attachments.mark_copy_published
    owner = production_fork_container.checkpointer.forks
    marker = "mark_published_fork" if creation_kind == "new_session_main" else "mark_published_child_fork"
    original_callback = getattr(owner, marker)
    callback_threads: list[int] = []
    mark_threads: list[int] = []
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fork-confirmation")
    pool_step = NativeStep()
    queued = asyncio.Event()
    tasks: list[Awaitable[object]] = []
    worker = confirmation.run_sync_shielded

    async def callback(*args, **kwargs) -> None:
        callback_threads.append(threading.get_ident())
        await original_callback(*args, **kwargs)

    def mark(capture, *, publication_preimage_hash: str) -> None:
        mark_threads.append(threading.get_ident())
        assert capture.creation_key == key
        if stage != "after":
            native_step.block()
        original_mark(capture, publication_preimage_hash=publication_preimage_hash)
        if stage == "after":
            native_step.block()

    async def queue_worker(operation, *args, **kwargs):
        tasks.append(loop.run_in_executor(None, pool_step.block))
        await _entered(pool_step)
        queued.set()
        return await worker(operation, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(attachments, "mark_copy_published", mark)
            patch.setattr(owner, marker, callback)
            if stage == "pool":
                patch.setattr(loop, "_default_executor", executor)
                patch.setattr(confirmation, "run_sync_shielded", queue_worker)
            creating = asyncio.create_task(context.forks.fork(request))
            tasks.append(creating)
            try:
                if stage == "pool":
                    await asyncio.wait_for(queued.wait(), 2)
                    assert not mark_threads
                else:
                    await _entered(native_step)
                capture = catalog.get_fork_source_capture(creation_key=key)
                assert capture is not None and capture.state == "materialized"
                assert capture.source_session_id == source.session_id
                target = resolver.resolve_session_node(capture.target_session_id)
                if creation_kind == "new_session_main":
                    frozen = catalog.get_creation_record(key)
                    target_thread = target / "threads" / frozen.main_thread_id
                else:
                    with closing(SessionControlStore(target / "session-control.sqlite", read_only=True)) as control:
                        frozen = control.get_thread_creation_record(key)
                    target_thread = target / frozen.final_relative_locator
                assert frozen.state == "published" and frozen.abort_reason is None
                target_canonical = target_thread / "rollout/rollout.jsonl"
                target_bytes = target_canonical.read_bytes()
                assert target_bytes == source_bytes
                claims = attachments._catalog.fork_attachment_claim_ids(key)
                assert claims
                with closing(SessionControlStore(target / "session-control.sqlite", read_only=True)) as control:
                    settlements = [row for row in read_copy_attachment_settlements(control) if row.copy_operation_id == key]
                assert len(settlements) == 1
                assert settlements[0].state == ("published_pending_attachment_commit" if stage == "after" else "preparing")
                gates = [NavigationTopologyGate(resolver.sessions_root).exclusive(),
                         SessionLifecycleGate(resolver.sessions_root).exclusive(capture.target_session_id)]
                entered = [asyncio.Event() for _ in gates]

                async def contend(index: int) -> None:
                    async with gates[index]:
                        entered[index].set()

                tasks.extend(asyncio.create_task(contend(index)) for index in range(len(gates)))
                creating.cancel("取消原fork发布确认")
                await _turns()
                assert not creating.done() and not any(event.is_set() for event in entered)
                assert callback_threads == [loop_thread]
                pool_step.release.set()
                if stage == "pool":
                    await _entered(native_step)
                    assert not creating.done() and not any(event.is_set() for event in entered)
                native_step.release.set()
                with pytest.raises(asyncio.CancelledError, match="取消原fork发布确认"):
                    await creating
                await asyncio.gather(*tasks, return_exceptions=True)
                assert all(event.is_set() for event in entered)
                assert len(mark_threads) == 1 and mark_threads[0] != loop_thread
                with closing(SessionControlStore(target / "session-control.sqlite", read_only=True)) as control:
                    assert [row.state for row in read_copy_attachment_settlements(control) if row.copy_operation_id == key] == ["published_pending_attachment_commit"]
                assert catalog.get_fork_source_capture(creation_key=key) == capture
                assert canonical.read_bytes() == source_bytes and target_canonical.read_bytes() == target_bytes
            finally:
                pool_step.release.set()
                native_step.release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                executor.shutdown(wait=True)

        async def no_materialization(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("发布确认重试不能再次物化目标")

        with monkeypatch.context() as patch:
            patch.setattr(context.forks, "_materialize", no_materialization)
            result = await context.forks.fork(request)
            assert await context.forks.fork(request) == result
        assert result.session.session_id == capture.target_session_id and result.thread_id == capture.target_thread_id
        if creation_kind == "new_session_main":
            assert catalog.get_creation_record(key) == frozen
        with closing(SessionControlStore(target / "session-control.sqlite", read_only=True)) as control:
            if creation_kind == "new_child_thread":
                assert control.get_thread_creation_record(key) == frozen
                assert control.connection.execute("SELECT COUNT(*) FROM thread_catalog").fetchone()[0] == 2
            assert not control.list_initial_execution_intents()
            assert [row.state for row in read_copy_attachment_settlements(control) if row.copy_operation_id == key] == ["committed"]
        assert attachments._catalog.fork_attachment_claim_ids(key) == claims
        assert catalog.get_fork_source_capture(creation_key=key) == capture
        assert canonical.read_bytes() == source_bytes and target_canonical.read_bytes() == target_bytes
    finally:
        # 断言失败仍经原产品入口结清同一操作及正式 watchdog。
        await context.forks.fork(request)
