"""附件恢复的原生读取和 pin 结算不占用事件循环，取消不提前释放准入。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import portalocker
import pytest

from app.core.resource_uri import parse_resource_uri
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import (
    get_current_request_id,
    reset_current_request_id,
    set_current_request_id,
)
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog import (
    recovery as recovery_module,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

SESSION_ID = "ses_019b781159d47374805bc4427d081a14"
PEER_ID = "ses_019b9cdd1ad5758788a18bb227323523"
REQUEST_ID = "request-attachment-recovery"


@dataclass(frozen=True)
class RecoveryCase:
    store: AttachmentBlobStore
    messages: RolloutCheckpointSaver
    ref: AttachmentRef


@pytest.fixture
async def recovery_case(tmp_path: Path, session_bundle_factory) -> AsyncIterator[RecoveryCase]:
    for identity in (SESSION_ID, PEER_ID):
        session_bundle_factory(tmp_path / ".boxteam/sessions", identity)
    with closing(AttachmentBlobStore(tmp_path, leases=ExternalResourceLeaseLedger(state_path=tmp_path / ".boxteam/resources.json"))) as store:
        ref, = await store.persist_inline(SESSION_ID, [AttachmentRef(
            file_id="inline:recovery.txt", name="recovery.txt", content_type="text/plain",
            data_url="data:text/plain;base64,Y3dk",
        )], thread_id=store._path_resolver.main_thread_id(SESSION_ID), message_id="msg_recovery")
        yield RecoveryCase(store, RolloutCheckpointSaver(tmp_path / ".boxteam/sessions", attachment_store=store), ref)


@pytest.mark.parametrize("stage", ["catalog", "canonical", "settle"])
@pytest.mark.parametrize("outcome", ["success", "cancel", "failure", "cancel_failure"])
async def test_recovery_retains_guards_context_and_original_failure(
    recovery_case: RecoveryCase, monkeypatch: pytest.MonkeyPatch, stage: str, outcome: str,
) -> None:
    case, store = recovery_case, recovery_case.store
    loop, event_thread = asyncio.get_running_loop(), threading.get_ident()
    entered, release = asyncio.Event(), threading.Event()
    expired: list[bool] = []
    failure = OSError("附件恢复原生访问失败")
    failure.add_note("保留原 pin 与原异常")
    owner, method = {
        "catalog": (store._path_resolver.catalog_store, "find_operation_replay_delete"),
        "canonical": (case.messages, "attachment_message_references"),
        "settle": (store, "_settle_pin"),
    }[stage]
    operation = getattr(owner, method)
    calls = 0

    def block(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert get_current_request_id() == REQUEST_ID
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                expired.append(True)
                raise TimeoutError("附件恢复阻塞了事件循环")
            assert threading.get_ident() != event_thread
            if "failure" in outcome:
                raise failure
        return operation(*args, **kwargs)

    token = set_current_request_id(REQUEST_ID)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, method, block)
            pending = asyncio.create_task(store.recover_idle_session(SESSION_ID, messages=case.messages))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert not expired and not pending.done()
                async with store.deletion_holder.lock_for(SESSION_ID, wait=False) as holder:
                    assert not holder.acquired
                with (
                    store._gate.exclusive(SESSION_ID).path.open("r+b") as handle,
                    pytest.raises(portalocker.AlreadyLocked),
                ):
                    portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                assert await store.recover_idle_session(PEER_ID, messages=case.messages) == ()
                if "cancel" in outcome:
                    pending.cancel("取消附件恢复")
                    await asyncio.sleep(0)
                    pending.cancel("重复取消")
                    await asyncio.sleep(0)
                    assert not pending.done()
                    async with store.deletion_holder.lock_for(SESSION_ID, wait=False) as holder:
                        assert not holder.acquired
                release.set()
                if "failure" in outcome:
                    with pytest.raises(OSError) as caught:
                        await pending
                    assert caught.value is failure and failure.__notes__ == ["保留原 pin 与原异常"]
                    if "cancel" in outcome:
                        assert isinstance(failure.__context__, asyncio.CancelledError)
                        assert failure.__context__.args == ("取消附件恢复",)
                elif outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError, match="取消附件恢复"):
                        await pending
                else:
                    assert await pending == ("released-unused-reference", "settled-pin")
                async with store.deletion_holder.lock_for(SESSION_ID, wait=False) as holder:
                    assert holder.acquired
                with closing(store._open_control_store(SESSION_ID)) as control:
                    pins = control.list_non_terminal_leases()
                    assert len(pins) == int("failure" in outcome)
                # 失败留下的原 pin 可在同一入口重试；成功或已取消的结算不重复。
                retried = await store.recover_idle_session(SESSION_ID, messages=case.messages)
                assert retried == (("released-unused-reference", "settled-pin") if pins else ())
                attachment_id = parse_resource_uri(case.ref.file_id).attachment_id
                assert attachment_id is not None
                record = store._catalog.get_directory(attachment_id)
                assert record is not None and record.release_pending == 0 and record.availability == "released"
                assert store.read(SESSION_ID, case.ref.file_id).data == b"cwd"
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        reset_current_request_id(token)


@pytest.mark.parametrize("cancel", [False, True])
async def test_startup_snapshot_finishes_before_recovery_or_cancellation(
    recovery_case: RecoveryCase, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    case, store = recovery_case, recovery_case.store
    loop, event_thread = asyncio.get_running_loop(), threading.get_ident()
    entered, release = asyncio.Event(), threading.Event()
    expired: list[bool] = []
    catalog = store._path_resolver.catalog_store
    snapshot = catalog.read_nodes_snapshot

    def read_snapshot():
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(3):
            expired.append(True)
            raise TimeoutError("启动快照阻塞事件循环")
        assert threading.get_ident() != event_thread
        return snapshot()

    with monkeypatch.context() as patch:
        patch.setattr(catalog, "read_nodes_snapshot", read_snapshot)
        pending = asyncio.create_task(store.recover_pending_ingests(messages=case.messages))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert not expired and not pending.done()
            if cancel:
                pending.cancel("取消启动快照")
                await asyncio.sleep(0)
                pending.cancel("重复取消")
                await asyncio.sleep(0)
                assert not pending.done()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError, match="取消启动快照"):
                    await pending
            else:
                assert await pending == 1
            with closing(store._open_control_store(SESSION_ID)) as control:
                assert len(control.list_non_terminal_leases()) == int(cancel)
            assert await store.recover_pending_ingests(messages=case.messages) == int(cancel)
            assert store.read(SESSION_ID, case.ref.file_id).data == b"cwd"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("startup", [False, True])
async def test_real_catalog_lock_does_not_block_event_loop(
    recovery_case: RecoveryCase, startup: bool,
) -> None:
    case, store = recovery_case, recovery_case.store
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    expired: list[bool] = []

    def lock_catalog() -> None:
        with store._path_resolver.catalog_store._connection_lock:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                expired.append(True)

    with ThreadPoolExecutor(max_workers=1) as executor:
        occupying = loop.run_in_executor(executor, lock_catalog)
        await asyncio.wait_for(entered.wait(), 2)
        pending = asyncio.create_task(store.recover_pending_ingests(messages=case.messages) if startup
                                      else store.recover_idle_session(SESSION_ID, messages=case.messages))
        try:
            await asyncio.sleep(0.05)
            assert not expired and not pending.done()
            pending.cancel("等待真实 catalog 锁时取消")
            await asyncio.sleep(0)
            assert not pending.done()
            release.set()
            with pytest.raises(asyncio.CancelledError, match="等待真实 catalog 锁时取消"):
                await pending
            assert await store.recover_pending_ingests(messages=case.messages) == int(startup)
        finally:
            release.set()
            await asyncio.gather(pending, occupying, return_exceptions=True)


async def test_queued_recovery_cancellation_keeps_original_guards(
    recovery_case: RecoveryCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, store = recovery_case, recovery_case.store
    loop = asyncio.get_running_loop()
    entered, queued, release = asyncio.Event(), asyncio.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="attachment-recovery")
    occupying: asyncio.Future[None] | None = None

    def occupy() -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("附件恢复线程池屏障未释放")

    async def queue(operation, *args, **kwargs):
        nonlocal occupying
        occupying = loop.run_in_executor(None, occupy)
        await entered.wait()
        queued.set()
        return await run_sync_shielded(operation, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        patch.setattr(recovery_module, "run_sync_shielded", queue)
        pending = asyncio.create_task(store.recover_idle_session(SESSION_ID, messages=case.messages))
        try:
            await asyncio.wait_for(queued.wait(), 2)
            await asyncio.sleep(0)
            pending.cancel("排队恢复取消")
            await asyncio.sleep(0)
            pending.cancel("重复取消")
            await asyncio.sleep(0)
            assert not pending.done()
            async with store.deletion_holder.lock_for(SESSION_ID, wait=False) as holder:
                assert not holder.acquired
            with (
                store._gate.exclusive(SESSION_ID).path.open("r+b") as handle,
                pytest.raises(portalocker.AlreadyLocked),
            ):
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            release.set()
            with pytest.raises(asyncio.CancelledError, match="排队恢复取消"):
                await asyncio.wait_for(pending, 3)
            with closing(store._open_control_store(SESSION_ID)) as control:
                assert control.list_non_terminal_leases() == ()
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            if occupying is not None:
                await occupying
            executor.shutdown(wait=True)
