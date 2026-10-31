"""附件 cwd 的原生访问不阻塞事件循环，holder 覆盖 worker 排队和取消结算。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from app.core.resource_uri import parse_resource_uri
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import (
    get_current_gateway_id,
    get_current_request_id,
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog import store as store_module
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)

SESSION_ID = "ses_019b781159d47374805bc4427d081a14"
GATEWAY_ID = "gateway_00000000000040008000000000000099"
REQUEST_ID = "request-managed-cwd"


@pytest.fixture
async def cwd_store(tmp_path: Path, session_bundle_factory) -> AsyncIterator[tuple[AttachmentBlobStore, Path, str]]:
    session_bundle_factory(tmp_path / ".boxteam/sessions", SESSION_ID)
    with closing(AttachmentBlobStore(tmp_path)) as store:
        ref, = await store.persist_inline(SESSION_ID, [AttachmentRef(
            file_id="inline:cwd.txt", name="cwd.txt", content_type="text/plain", data_url="data:text/plain;base64,Y3dk",
        )], thread_id=store._path_resolver.main_thread_id(SESSION_ID), message_id="msg_cwd")
        assert ref.directory_uri is not None
        parsed = parse_resource_uri(ref.directory_uri)
        target = store.resolve_directory_uri(parsed)
        yield store, target, parsed.tail_segments[0]


@pytest.mark.parametrize("stage", ["lookup", "available", "stat"])
@pytest.mark.parametrize("outcome", ["success", "cancel", "failure"])
async def test_working_directory_keeps_context_holder_and_original_failure(
    cwd_store: tuple[AttachmentBlobStore, Path, str], monkeypatch: pytest.MonkeyPatch,
    stage: str, outcome: str,
) -> None:
    store, target, attachment_id = cwd_store
    before = store._catalog.get_directory(attachment_id)
    loop = asyncio.get_running_loop()
    event_thread = threading.get_ident()
    entered, release = asyncio.Event(), threading.Event()
    expired: list[bool] = []
    failure = OSError(5, "原生附件目录检查失败", str(target))
    published: list[str | None] = []
    reference, available, is_dir = store.reference_for_path, store._catalog.require_available_directory, Path.is_dir
    calls = 0

    def wait() -> None:
        assert get_current_gateway_id() == GATEWAY_ID and get_current_request_id() == REQUEST_ID
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(3):
            expired.append(True)
            raise TimeoutError("附件 cwd 检查阻塞事件循环")
        assert threading.get_ident() != event_thread
        if outcome == "failure":
            raise failure

    def lookup(path: Path) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1 and stage == "lookup":
            wait()
        return reference(path)

    def require(identity: str):
        if identity == attachment_id and stage == "available":
            wait()
        return available(identity)

    def stat(path: Path) -> bool:
        if path == target and stage == "stat":
            wait()
        return is_dir(path)

    async def admit() -> None:
        async with store.admit_working_directory(target) as uri:
            published.append(uri)

    token, request_token = set_current_gateway_id(GATEWAY_ID), set_current_request_id(REQUEST_ID)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, "reference_for_path", lookup)
            patch.setattr(store._catalog, "require_available_directory", require)
            patch.setattr(Path, "is_dir", stat)
            pending = asyncio.create_task(admit())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert not expired and not pending.done() and published == []
                async with store.admit_working_directory(target.parent / "ordinary") as peer:
                    assert peer is None
                async with store._directory_operations.lock_for(attachment_id, wait=False) as probe:
                    assert probe.acquired is (stage == "lookup")
                if outcome == "cancel":
                    assert pending.cancel("目录准入取消")
                    await asyncio.sleep(0)
                    assert pending.cancel("重复取消")
                    await asyncio.sleep(0)
                    assert not pending.done() and published == []
                    async with store._directory_operations.lock_for(attachment_id, wait=False) as probe:
                        assert probe.acquired is (stage == "lookup")
                release.set()
                if outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError, match="目录准入取消"):
                        await pending
                elif outcome == "failure":
                    with pytest.raises(OSError) as caught:
                        await pending
                    assert caught.value is failure
                else:
                    await pending
                    assert published == [parse_resource_uri(store.directory_uri(attachment_id), local_gateway_id=GATEWAY_ID).display_uri]
                if outcome != "success":
                    assert published == []
                async with store._directory_operations.lock_for(attachment_id, wait=False) as probe:
                    assert probe.acquired
                assert store._catalog.get_directory(attachment_id) == before
                assert (target / "original/content").read_bytes() == b"cwd"
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        reset_current_gateway_id(token)
        reset_current_request_id(request_token)


@pytest.mark.parametrize("stage", ["reference_for_path", "_require_working_directory"])
async def test_queued_working_directory_cancellation_keeps_original_holder(
    cwd_store: tuple[AttachmentBlobStore, Path, str], monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    store, target, attachment_id = cwd_store
    loop = asyncio.get_running_loop()
    entered, queued, release = asyncio.Event(), asyncio.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="attachment-cwd")
    occupying: asyncio.Future[None] | None = None
    published: list[str | None] = []

    def occupy() -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("附件 cwd 线程池屏障未释放")

    async def queue(operation, *args, **kwargs):
        nonlocal occupying
        if operation.__name__ == stage:
            occupying = loop.run_in_executor(None, occupy)
            await entered.wait()
            queued.set()
        return await run_sync_shielded(operation, *args, **kwargs)

    async def admit() -> None:
        async with store.admit_working_directory(target) as uri:
            published.append(uri)

    with monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        patch.setattr(store_module, "run_sync_shielded", queue)
        pending = asyncio.create_task(admit())
        try:
            await asyncio.wait_for(queued.wait(), 2)
            await asyncio.sleep(0)
            pending.cancel("排队准入取消")
            await asyncio.sleep(0)
            pending.cancel("重复取消")
            await asyncio.sleep(0)
            assert not pending.done() and published == []
            async with store._directory_operations.lock_for(attachment_id, wait=False) as probe:
                assert probe.acquired is (stage == "reference_for_path")
            release.set()
            with pytest.raises(asyncio.CancelledError, match="排队准入取消"):
                await asyncio.wait_for(pending, 3)
            assert published == []
            async with store._directory_operations.lock_for(attachment_id, wait=False) as probe:
                assert probe.acquired
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            if occupying is not None:
                await occupying
            executor.shutdown(wait=True)
