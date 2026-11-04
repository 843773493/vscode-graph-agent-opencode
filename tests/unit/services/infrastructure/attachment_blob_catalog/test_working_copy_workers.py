"""工作副本 的同步查询、删除与账本写入必须在原 holder 内结算。"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
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
from app.services.infrastructure.attachment_blob_catalog.directories import (
    remove_directory,
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
REQUEST_ID = "request-attachment-working-copy"


@dataclass(frozen=True)
class WorkingCopyCase:
    store: AttachmentBlobStore
    leases: ExternalResourceLeaseLedger
    attachment_id: str
    directory: Path
    ledger_path: Path


@pytest.fixture
async def copy_case(tmp_path: Path, session_bundle_factory) -> AsyncIterator[WorkingCopyCase]:
    session_bundle_factory(tmp_path / ".boxteam/sessions", SESSION_ID)
    ledger_path = tmp_path / ".boxteam/resources.json"
    leases = ExternalResourceLeaseLedger(state_path=ledger_path)
    with closing(AttachmentBlobStore(tmp_path, leases=leases)) as store:
        ref, = await store.persist_inline(SESSION_ID, [AttachmentRef(
            file_id="inline:gc.txt", name="gc.txt", content_type="text/plain",
            data_url="data:text/plain;base64,Y3dk",
        )], thread_id=store._path_resolver.main_thread_id(SESSION_ID), message_id="msg_gc_worker")
        parsed = parse_resource_uri(ref.file_id)
        assert parsed.attachment_id is not None
        assert ref.directory_uri is not None
        directory = store.resolve_directory_uri(parse_resource_uri(ref.directory_uri))
        store.release_session_references(SESSION_ID)
        leases.register(resource_id=parsed.attachment_id, kind="attachment_directory", lifetime_scope="workspace")
        yield WorkingCopyCase(store, leases, parsed.attachment_id, directory, ledger_path)


@pytest.mark.parametrize("boundary", ["sync", "async"])
@pytest.mark.parametrize("failure", ["value", "io", "cancel"])
async def test_access_settlement_preserves_body_and_cleanup_errors(
    copy_case: WorkingCopyCase, monkeypatch: pytest.MonkeyPatch, boundary: str, failure: str,
) -> None:
    store = copy_case.store
    primary = {"value": ValueError("原正文不是普通文件"), "io": OSError("原正文读取失败"),
               "cancel": asyncio.CancelledError("原访问已取消")}[failure]
    cleanup = RuntimeError("原待处理结清失败")
    primary.add_note("主体错误身份与诊断不能丢失")

    def fail(_identity: str) -> None:
        raise cleanup

    monkeypatch.setattr(store, "_settle_directory_access", fail)
    with pytest.raises(BaseExceptionGroup) as caught:
        if boundary == "sync":
            with store.access_files(copy_case.attachment_id):
                raise primary
        else:
            async with store.admit_directory(copy_case.attachment_id):
                raise primary
    assert caught.value.exceptions == (primary, cleanup)
    assert primary.__notes__ == ["主体错误身份与诊断不能丢失"]
    record = store._catalog.get_directory(copy_case.attachment_id)
    assert record is not None and record.release_pending == 1 and copy_case.directory.is_dir()


@pytest.mark.parametrize("kind", ["file", "directory", "fifo", "symlink"])
async def test_new_native_results_are_retained_without_opening_them(copy_case: WorkingCopyCase, kind: str) -> None:
    result = copy_case.directory / "derived/native-result"
    if kind == "file":
        result.write_bytes(b"native processing result")
    elif kind == "directory":
        result.mkdir()
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("当前平台没有原生 FIFO")
        os.mkfifo(result)
    else:
        if os.name == "nt":
            pytest.skip("Windows symlink 权限由实机专项验证")
        result.symlink_to(copy_case.directory / "original/content")
    assert await copy_case.store.process_pending_directories() == ()
    record = copy_case.store._catalog.get_directory(copy_case.attachment_id)
    assert record is not None and record.availability == "available" and record.release_pending == 0
    assert record.release_reason == "目录相对上传初始基线有修改，保留原位置与处理结果"
    assert result.exists() and copy_case.directory.is_dir()


async def test_missing_available_directory_remains_an_explicit_failure(copy_case: WorkingCopyCase) -> None:
    remove_directory(copy_case.directory)
    with pytest.raises(FileNotFoundError, match="附件目录缺失"):
        await copy_case.store.process_pending_directories()
    record = copy_case.store._catalog.get_directory(copy_case.attachment_id)
    assert record is not None and record.availability == "available" and record.release_pending == 1
    assert "FileNotFoundError" in record.release_reason


@pytest.mark.parametrize("count", [1, 32])
async def test_working_copy_candidates_use_one_validated_catalog_snapshot(copy_case: WorkingCopyCase, count: int) -> None:
    store, catalog = copy_case.store, copy_case.store._catalog
    identities = {copy_case.attachment_id}
    for index in range(1, count):
        ref, = await store.persist_inline(SESSION_ID, [AttachmentRef(
            file_id=f"inline:gc-{index}.txt", name=f"gc-{index}.txt", content_type="text/plain",
            data_url="data:text/plain;base64,Y3dk",
        )], thread_id=store._path_resolver.main_thread_id(SESSION_ID), message_id=f"msg_gc_snapshot_{index}")
        assert ref.directory_uri is not None
        identities.add(parse_resource_uri(ref.directory_uri).tail_segments[0])
    assert len(identities) == count
    store.release_session_references(SESSION_ID)
    statements: list[str] = []
    catalog.connection.set_trace_callback(statements.append)
    try:
        candidates = catalog.pending_directories()
    finally:
        catalog.connection.set_trace_callback(None)
    assert {record.attachment_id for record in candidates} == identities
    assert len([statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]) == 1
    assert candidates == tuple(catalog.get_directory(identity) for identity in sorted(identities))


async def test_local_fork_claim_hands_retention_to_target_runtime_without_export(fork_capture) -> None:
    store, record = fork_capture
    ref, = await store.persist_inline(record.source_session_id, [AttachmentRef(
        file_id="inline:local-fork.txt", name="local-fork.txt", content_type="text/plain",
        data_url="data:text/plain;base64,Y3dk",
    )], thread_id=record.source_thread_id, message_id="msg_local_fork_gc")
    snapshot, = store.capture_fork_attachments(record, same_storage_owner=True, references=((ref.file_id, True),))
    assert snapshot.source_attachment_id == snapshot.target_attachment_id
    store.release_session_references(record.source_session_id)
    leases = ExternalResourceLeaseLedger(state_path=store._workspace_root / ".boxteam/resources.json")
    assert await store.process_pending_directories(leases=leases) == ()  # prepared claim 保护发布前的来源。
    claims = store._catalog.fork_attachment_claim_ids(record.copy_operation_id)
    store._catalog.reserve_fork_attachment_owners(copy_operation_id=record.copy_operation_id, claim_ids=claims)
    assert await store.process_pending_directories(leases=leases) == ()
    store._catalog.settle_fork_attachment_claims(
        copy_operation_id=record.copy_operation_id, claim_ids=claims, state="committed", now=store._clock(),
    )
    assert await store.process_pending_directories(leases=leases) == ()  # committed 后由目标活动关联保活。
    token = "runtime:local-fork-gc:1"
    store.bind_thread_runtime(record.target_session_id, record.target_thread_id, token)
    store._catalog.release_associations(
        session_id=record.target_session_id, thread_id=record.target_thread_id, binding_token=token, now=store._clock(),
    )
    directory = store._catalog.get_directory(snapshot.target_attachment_id)
    assert directory.release_pending == 1
    assert await store.process_pending_directories(leases=leases) == (snapshot.target_attachment_id,)
    assert tuple(store._catalog.connection.execute(
        "SELECT state,export_released FROM attachment_directory_fork_claims",
    ).fetchone()) == ("committed", 0)


async def test_cross_workspace_export_claim_retains_source_until_original_release(fork_import_target) -> None:
    case = fork_import_target
    source, record = case.source, case.record
    snapshot, = case.snapshots
    assert snapshot.source_attachment_id != snapshot.target_attachment_id
    source.release_session_references(record.source_session_id)
    leases = ExternalResourceLeaseLedger(state_path=source._workspace_root / ".boxteam/resources.json")
    directory = source._catalog.get_directory(snapshot.source_attachment_id)
    assert directory.release_pending == 1
    assert await source.process_pending_directories(leases=leases) == ()
    await source.release_fork_export(record)
    assert source._catalog.get_directory(snapshot.source_attachment_id).availability == "released"


@pytest.mark.parametrize("stage", ["candidates", "ledger", "finish"])
@pytest.mark.parametrize("outcome", ["success", "cancel", "failure", "cancel_failure"])
async def test_working_copy_storage_keeps_loop_holder_and_recoverable_publication(
    copy_case: WorkingCopyCase, monkeypatch: pytest.MonkeyPatch, stage: str, outcome: str,
) -> None:
    case, store = copy_case, copy_case.store
    loop, event_thread = asyncio.get_running_loop(), threading.get_ident()
    entered, release = asyncio.Event(), threading.Event()
    failure = OSError("目录 工作副本 原存储访问失败")
    failure.add_note("原 locator 必须可重试")
    owner, method = {
        "candidates": (store._catalog, "pending_directories"),
        "ledger": (case.leases, "update_external_state"),
        "finish": (store._catalog, "set_working_copy_state"),
    }[stage]
    original = getattr(owner, method)
    calls = 0

    def block(*args: object, **kwargs: object) -> object:
        nonlocal calls
        if stage == "finish" and kwargs.get("state") != "released":
            return original(*args, **kwargs)
        calls += 1
        if calls == 1:
            assert get_current_request_id() == REQUEST_ID
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise TimeoutError("目录 工作副本 阻塞事件循环")
            assert threading.get_ident() != event_thread
            if "failure" in outcome:
                raise failure
        return original(*args, **kwargs)

    request = set_current_request_id(REQUEST_ID)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, method, block)
            pending = asyncio.create_task(store.process_pending_directories(leases=case.leases))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert not pending.done()
                async with store._directory_operations.lock_for(case.attachment_id, wait=False) as holder:
                    assert holder.acquired is (stage == "candidates")
                if stage != "candidates":
                    assert (deleting := store._catalog.get_directory(case.attachment_id)) is not None
                    assert deleting.availability == "releasing"
                    assert not case.directory.exists()
                if "cancel" in outcome:
                    pending.cancel("取消目录 工作副本")
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
                        assert failure.__context__.args == ("取消目录 工作副本",)
                elif outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError, match="取消目录 工作副本"):
                        await pending
                else:
                    assert await pending == (case.attachment_id,)
                untouched = stage == "candidates" and outcome != "success"
                retryable = "failure" in outcome
                record = store._catalog.get_directory(case.attachment_id)
                assert record is not None
                assert record.availability == ("available" if untouched else "releasing" if retryable else "released")
                assert case.directory.exists() is untouched
                state = "running" if untouched or (stage == "ledger" and retryable) else "stopped"
                assert (registered := case.leases.get(case.attachment_id)) is not None
                assert registered.status == state
                cold = ExternalResourceLeaseLedger(state_path=case.ledger_path)
                assert (persisted := cold.get(case.attachment_id)) is not None
                assert persisted.status == state
                assert await store.process_pending_directories(leases=case.leases) == ((case.attachment_id,) if untouched or retryable else ())
                assert await store.process_pending_directories(leases=case.leases) == ()
                assert (final := store._catalog.get_directory(case.attachment_id)) is not None
                assert final.availability == "released"
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
    copy_case: WorkingCopyCase, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    case, store = copy_case, copy_case.store
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def retained(*_args: object, **_kwargs: object) -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(3):
            raise TimeoutError("测试未归还保留目录核对屏障")
        raise AttachmentDirectoryRetainedError("目录在候选查询后重新关联")

    with monkeypatch.context() as patch:
        patch.setattr(store._catalog, "require_releasable_directory", retained)
        pending = asyncio.create_task(store.process_pending_directories(leases=case.leases))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if cancel:
                pending.cancel("取消仍被保留目录的 工作副本")
                await asyncio.sleep(0)
                pending.cancel("重复取消")
                await asyncio.sleep(0)
                assert not pending.done()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError, match="取消仍被保留目录的 工作副本"):
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
    copy_case: WorkingCopyCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, store = copy_case, copy_case.store
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    original = case.leases.update_external_state
    independent = store._workspace_root / "independent.txt"

    def blocked_ledger(resource_id: str, *, lifetime_scope: str, status: str,
                       unresolved_lease_id: str | None = None) -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(3):
            raise TimeoutError("测试未归还 工作副本 账本屏障")
        original(resource_id, lifetime_scope=lifetime_scope, status=status, unresolved_lease_id=unresolved_lease_id)

    def write_independent() -> None:
        with workspace_file_mutation_lock(store._workspace_root):
            independent.write_bytes(b"independent-file-operation")

    with monkeypatch.context() as patch:
        patch.setattr(case.leases, "update_external_state", blocked_ledger)
        pending = asyncio.create_task(store.process_pending_directories(leases=case.leases))
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


async def test_working_copy_real_ledger_file_failure_retains_deletion_for_same_entry_retry(copy_case: WorkingCopyCase) -> None:
    case, store = copy_case, copy_case.store
    record = store._catalog.get_directory(case.attachment_id)
    assert record is not None
    temporary = case.ledger_path.with_suffix(".tmp")
    temporary.mkdir()
    with pytest.raises((IsADirectoryError, PermissionError)):
        await store.process_pending_directories(leases=case.leases)
    pending = store._catalog.get_directory(case.attachment_id)
    assert pending is not None
    assert pending.availability == "releasing"
    assert pending.relative_locator == record.relative_locator and pending.release_pending == 1
    assert not case.directory.exists()
    assert (registered := case.leases.get(case.attachment_id)) is not None
    assert registered.status == "running"
    cold = ExternalResourceLeaseLedger(state_path=case.ledger_path)
    assert (persisted := cold.get(case.attachment_id)) is not None
    assert persisted.status == "running"
    temporary.rmdir()
    assert await store.process_pending_directories(leases=case.leases) == (case.attachment_id,)
    assert (final := store._catalog.get_directory(case.attachment_id)) is not None
    assert final.availability == "released"
    cold = ExternalResourceLeaseLedger(state_path=case.ledger_path)
    assert (stopped := cold.get(case.attachment_id)) is not None
    assert stopped.status == "stopped"
    assert await store.process_pending_directories(leases=case.leases) == ()


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
