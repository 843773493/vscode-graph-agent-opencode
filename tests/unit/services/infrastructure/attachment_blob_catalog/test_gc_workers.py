"""GC 的同步查询、删除与账本写入必须在原 holder 内结算。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import (
    get_current_request_id,
    reset_current_request_id,
    set_current_request_id,
)
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentDirectoryRetainedError,
)
from app.services.infrastructure.attachment_blob_catalog.fork_import import _import_id
from app.services.infrastructure.attachment_blob_catalog.locator import (
    ingest_staging_relative_locator,
    resolve_staging_path,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.workspace_files.mutation_lock import (
    workspace_file_mutation_lock,
)
from tests.unit.services.infrastructure.test_attachment_blob_catalog import (
    fork_capture as fork_capture,  # noqa: PLC0414
)
from tests.unit.services.infrastructure.test_attachment_blob_catalog import (
    fork_import_target as fork_import_target,  # noqa: PLC0414
)

SESSION_ID = "ses_019b781159d47374805bc4427d081a14"
REQUEST_ID = "request-attachment-gc"


@dataclass(frozen=True)
class GcCase:
    store: AttachmentBlobStore
    leases: ExternalResourceLeaseLedger
    attachment_id: str
    directory: Path
    ledger_path: Path


@pytest.fixture
async def gc_case(tmp_path: Path, session_bundle_factory) -> AsyncIterator[GcCase]:
    session_bundle_factory(tmp_path / ".boxteam/sessions", SESSION_ID)
    with closing(AttachmentBlobStore(tmp_path)) as store:
        ref, = await store.persist_inline(SESSION_ID, [AttachmentRef(
            file_id="inline:gc.txt", name="gc.txt", content_type="text/plain",
            data_url="data:text/plain;base64,Y3dk",
        )], thread_id=store._path_resolver.main_thread_id(SESSION_ID), message_id="msg_gc_worker")
        parsed = parse_resource_uri(ref.file_id)
        assert parsed.attachment_id is not None
        assert ref.directory_uri is not None
        directory = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
        store.release_session_references(SESSION_ID)
        now = datetime.now(UTC) + timedelta(days=31)
        store._clock = lambda: now
        ledger_path = tmp_path / ".boxteam/resources.json"
        leases = ExternalResourceLeaseLedger(state_path=ledger_path)
        leases.register(resource_id=parsed.attachment_id, kind="attachment_directory", lifetime_scope="workspace")
        yield GcCase(store, leases, parsed.attachment_id, directory, ledger_path)


@pytest.mark.parametrize("stage", ["candidates", "ledger", "finish"])
@pytest.mark.parametrize("outcome", ["success", "cancel", "failure", "cancel_failure"])
async def test_gc_storage_keeps_loop_holder_and_recoverable_publication(
    gc_case: GcCase, monkeypatch: pytest.MonkeyPatch, stage: str, outcome: str,
) -> None:
    case, store = gc_case, gc_case.store
    loop, event_thread = asyncio.get_running_loop(), threading.get_ident()
    entered, release = asyncio.Event(), threading.Event()
    failure = OSError("目录 GC 原存储访问失败")
    failure.add_note("原 locator 必须可重试")
    owner, method = {
        "candidates": (store._catalog, "gc_candidates"),
        "ledger": (case.leases, "update_external_state"),
        "finish": (store._catalog, "finish_delete"),
    }[stage]
    original = getattr(owner, method)
    calls = 0

    def block(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert get_current_request_id() == REQUEST_ID
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise TimeoutError("目录 GC 阻塞事件循环")
            assert threading.get_ident() != event_thread
            if "failure" in outcome:
                raise failure
        return original(*args, **kwargs)

    request = set_current_request_id(REQUEST_ID)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, method, block)
            pending = asyncio.create_task(store.collect_garbage(leases=case.leases))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert not pending.done()
                async with store._directory_operations.lock_for(case.attachment_id, wait=False) as holder:
                    assert holder.acquired is (stage == "candidates")
                if stage != "candidates":
                    assert (deleting := store._catalog.get_directory(case.attachment_id)) is not None
                    assert deleting.availability == "deleting"
                    assert not case.directory.exists()
                if "cancel" in outcome:
                    pending.cancel("取消目录 GC")
                    await asyncio.sleep(0)
                    pending.cancel("重复取消")
                    await asyncio.sleep(0)
                    assert not pending.done()
                    async with store._directory_operations.lock_for(case.attachment_id, wait=False) as holder:
                        assert holder.acquired is (stage == "candidates")
                release.set()
                if "failure" in outcome:
                    with pytest.raises(OSError) as caught:
                        await pending
                    assert caught.value is failure and failure.__notes__ == ["原 locator 必须可重试"]
                    if "cancel" in outcome:
                        assert isinstance(failure.__context__, asyncio.CancelledError)
                        assert failure.__context__.args == ("取消目录 GC",)
                elif outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError, match="取消目录 GC"):
                        await pending
                else:
                    assert await pending == (case.attachment_id,)
                untouched = stage == "candidates" and outcome != "success"
                retryable = "failure" in outcome
                record = store._catalog.get_directory(case.attachment_id)
                assert record is not None
                assert record.availability == ("available" if untouched else "deleting" if retryable else "tombstoned")
                assert case.directory.exists() is untouched
                state = "running" if untouched or (stage == "ledger" and retryable) else "stopped"
                assert (registered := case.leases.get(case.attachment_id)) is not None
                assert registered.status == state
                cold = ExternalResourceLeaseLedger(state_path=case.ledger_path)
                assert (persisted := cold.get(case.attachment_id)) is not None
                assert persisted.status == state
                assert await store.collect_garbage(leases=case.leases) == ((case.attachment_id,) if untouched or retryable else ())
                assert await store.collect_garbage(leases=case.leases) == ()
                assert (final := store._catalog.get_directory(case.attachment_id)) is not None
                assert final.availability == "tombstoned"
                assert (stopped := case.leases.get(case.attachment_id)) is not None
                assert stopped.status == "stopped"
                assert not case.directory.exists()
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        reset_current_request_id(request)


@pytest.mark.parametrize("cancel", [False, True])
async def test_retained_directory_skip_does_not_consume_gc_cancellation(
    gc_case: GcCase, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    case, store = gc_case, gc_case.store
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def retained(*_args: object, **_kwargs: object) -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(3):
            raise TimeoutError("测试未归还保留目录核对屏障")
        raise AttachmentDirectoryRetainedError("目录在候选查询后重新关联")

    with monkeypatch.context() as patch:
        patch.setattr(store._catalog, "begin_delete", retained)
        pending = asyncio.create_task(store.collect_garbage(leases=case.leases))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if cancel:
                pending.cancel("取消仍被保留目录的 GC")
                await asyncio.sleep(0)
                pending.cancel("重复取消")
                await asyncio.sleep(0)
                assert not pending.done()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError, match="取消仍被保留目录的 GC"):
                    await pending
            else:
                assert await pending == ()
            assert (record := store._catalog.get_directory(case.attachment_id)) is not None
            assert record.availability == "available"
            assert (case.directory / "original/content").read_bytes() == b"cwd"
            assert (registered := case.leases.get(case.attachment_id)) is not None
            assert registered.status == "running"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_waiting_gc_ledger_does_not_hold_workspace_file_mutation_lock(
    gc_case: GcCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, store = gc_case, gc_case.store
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    original = case.leases.update_external_state
    independent = store._workspace_root / "independent.txt"

    def blocked_ledger(resource_id: str, *, lifetime_scope: str, status: str,
                       unresolved_lease_id: str | None = None) -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(3):
            raise TimeoutError("测试未归还 GC 账本屏障")
        original(resource_id, lifetime_scope=lifetime_scope, status=status, unresolved_lease_id=unresolved_lease_id)

    def write_independent() -> None:
        with workspace_file_mutation_lock(store._workspace_root):
            independent.write_bytes(b"independent-file-operation")

    with monkeypatch.context() as patch:
        patch.setattr(case.leases, "update_external_state", blocked_ledger)
        pending = asyncio.create_task(store.collect_garbage(leases=case.leases))
        writing = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            writing = asyncio.create_task(asyncio.to_thread(write_independent))
            await asyncio.wait_for(asyncio.shield(writing), 1)
            assert independent.read_bytes() == b"independent-file-operation"
            assert not pending.done()
            async with store._directory_operations.lock_for(case.attachment_id, wait=False) as holder:
                assert not holder.acquired
            release.set()
            assert await pending == (case.attachment_id,)
        finally:
            release.set()
            await asyncio.gather(pending, *([writing] if writing is not None else []), return_exceptions=True)


async def test_gc_real_ledger_file_failure_retains_deletion_for_same_entry_retry(gc_case: GcCase) -> None:
    case, store = gc_case, gc_case.store
    record = store._catalog.get_directory(case.attachment_id)
    assert record is not None
    temporary = case.ledger_path.with_suffix(".tmp")
    temporary.mkdir()
    with pytest.raises((IsADirectoryError, PermissionError)):
        await store.collect_garbage(leases=case.leases)
    pending = store._catalog.get_directory(case.attachment_id)
    assert pending is not None
    assert pending.availability == "deleting"
    assert (pending.relative_locator, pending.gc_after) == (record.relative_locator, record.gc_after)
    assert not case.directory.exists()
    assert (registered := case.leases.get(case.attachment_id)) is not None
    assert registered.status == "running"
    cold = ExternalResourceLeaseLedger(state_path=case.ledger_path)
    assert (persisted := cold.get(case.attachment_id)) is not None
    assert persisted.status == "running"
    temporary.rmdir()
    assert await store.collect_garbage(leases=case.leases) == (case.attachment_id,)
    assert (final := store._catalog.get_directory(case.attachment_id)) is not None
    assert final.availability == "tombstoned"
    cold = ExternalResourceLeaseLedger(state_path=case.ledger_path)
    assert (stopped := cold.get(case.attachment_id)) is not None
    assert stopped.status == "stopped"
    assert await store.collect_garbage(leases=case.leases) == ()


@pytest.mark.parametrize("stage", ["operations", "snapshots", "exists"])
@pytest.mark.parametrize("outcome", ["success", "cancel", "failure", "cancel_failure"])
async def test_terminal_fork_staging_storage_keeps_original_holder_and_outcome(
    fork_import_target, monkeypatch: pytest.MonkeyPatch, stage: str, outcome: str,
) -> None:
    case = fork_import_target
    store, record = case.target, case.record
    snapshot, = case.snapshots
    store._catalog.prepare_fork_attachment_claims(
        copy_operation_id=record.copy_operation_id, source_snapshot_id=record.snapshot_id,
        operation_preimage_hash=record.preimage_hash, snapshots=case.snapshots,
    )
    store._catalog.settle_fork_attachment_claims(
        copy_operation_id=record.copy_operation_id, claim_ids=(snapshot.claim_id,), state="aborted", now=store._clock(),
    )
    staging = resolve_staging_path(store._attachments_root, ingest_staging_relative_locator(
        _import_id(record.copy_operation_id, snapshot.target_attachment_id),
    ))
    staging.mkdir(parents=True)
    (staging / "partial.txt").write_bytes(b"original-partial-copy")
    unknown = staging.with_name("ing_" + "f" * 32)
    unknown.mkdir()
    (unknown / "keep.txt").write_bytes(b"unknown-writer")
    frozen = store._catalog.terminal_fork_attachment_snapshots(record.copy_operation_id)
    loop, event_thread = asyncio.get_running_loop(), threading.get_ident()
    entered, release = asyncio.Event(), threading.Event()
    failure = OSError("fork 暂存原访问失败")
    owner, method = {
        "operations": (store._catalog, "terminal_fork_attachment_operations"),
        "snapshots": (store._catalog, "terminal_fork_attachment_snapshots"),
        "exists": (Path, "exists"),
    }[stage]
    original = getattr(owner, method)
    calls = 0

    def block(*args: object, **kwargs: object) -> object:
        nonlocal calls
        if stage != "exists" or args[0] == staging:
            calls += 1
            if calls == 1:
                assert get_current_request_id() == REQUEST_ID
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise TimeoutError("fork 暂存清理阻塞事件循环")
                assert threading.get_ident() != event_thread
                if "failure" in outcome:
                    raise failure
        return original(*args, **kwargs)

    request = set_current_request_id(REQUEST_ID)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, method, block)
            pending = asyncio.create_task(store.collect_fork_import_staging())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert not pending.done()
                async with store._fork_imports.lock_for(record.copy_operation_id, wait=False) as holder:
                    assert holder.acquired is (stage == "operations")
                if "cancel" in outcome:
                    pending.cancel("取消 fork 暂存清理")
                    await asyncio.sleep(0)
                    pending.cancel("重复取消")
                    await asyncio.sleep(0)
                    assert not pending.done()
                    async with store._fork_imports.lock_for(record.copy_operation_id, wait=False) as holder:
                        assert holder.acquired is (stage == "operations")
                release.set()
                if "failure" in outcome:
                    with pytest.raises(OSError) as caught:
                        await pending
                    assert caught.value is failure
                    if "cancel" in outcome:
                        assert isinstance(failure.__context__, asyncio.CancelledError)
                        assert failure.__context__.args == ("取消 fork 暂存清理",)
                elif outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError, match="取消 fork 暂存清理"):
                        await pending
                else:
                    assert await pending == (snapshot.target_attachment_id,)
                retained = "failure" in outcome or (stage == "operations" and outcome == "cancel")
                assert staging.exists() is retained
                assert await store.collect_fork_import_staging() == ((snapshot.target_attachment_id,) if retained else ())
                assert await store.collect_fork_import_staging(record.copy_operation_id) == ()
                assert store._catalog.terminal_fork_attachment_snapshots(record.copy_operation_id) == frozen
                assert (unknown / "keep.txt").read_bytes() == b"unknown-writer"
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        reset_current_request_id(request)
