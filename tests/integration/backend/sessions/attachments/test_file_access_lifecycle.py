"""真实图卸载与普通文件访问竞争，最后保护解除后沿原 owner 收尾。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import zlib
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import workspace as workspace_api
from app.api.workspace import (
    get_workspace_file_content,
    reveal_workspace_file_entry,
    stream_workspace_file_events,
)
from app.api.workspace_file_response import WorkspaceFileResponse
from app.core.key_lock_pool import KEY_LOCK_SHARDS
from app.core.resource_uri import file_uri_child, parse_attachment_uri
from app.schemas.internal_v2.text_mutation import PatchText
from app.schemas.internal_v2.workspace import WorkspaceFileWatchRequest
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceFileMutationIntentDTO,
)
from app.services.infrastructure import workspace_service as service_module
from app.services.infrastructure.attachment_blob_catalog import ingest as ingest_module
from app.services.infrastructure.workspace_files import operations as operations_module
from tests.integration.backend.sessions.test_attachment_runtime_lifecycle import (
    attachment_directory,
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


@pytest.fixture
async def access_case(runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch):
    isolate_unused_external_managers(runtime_case, monkeypatch)
    session, _thread, ref = await execute_upload(runtime_case)
    store = runtime_case.runner.container.attachment_blob_store
    attachment_id = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
    assert attachment_id is not None and ref.directory_uri is not None
    return runtime_case, session, ref, attachment_id, attachment_directory(store, attachment_id)


@pytest.mark.parametrize("kind", ["content", "listing", "media"])
async def test_last_detach_during_data_access_is_processed_when_holder_exits(access_case, monkeypatch, kind: str) -> None:
    case, session, ref, aid, directory = access_case
    store, service = case.runner.container.attachment_blob_store, case.runner.container.workspace_service
    entered, release = threading.Event(), threading.Event()

    def pause() -> None:
        entered.set()
        if not release.wait(10):
            raise TimeoutError("正文访问屏障未释放")

    if kind == "content":
        read = service_module.read_file_preimage

        def content(*args, **kwargs):
            result = read(*args, **kwargs)
            pause()
            return result

        monkeypatch.setattr(service_module, "read_file_preimage", content)
        operation = service.get_file_content(path=ref.file_id)
    elif kind == "listing":
        scan = service._scan_directory

        def listing(*args, **kwargs):
            result = scan(*args, **kwargs)
            pause()
            return result

        monkeypatch.setattr(service, "_scan_directory", listing)
        operation = service.list_files(path=ref.directory_uri)
    else:
        read_bytes = Path.read_bytes

        def media(path: Path) -> bytes:
            result = read_bytes(path)
            if path == directory / "original/content":
                pause()
            return result

        monkeypatch.setattr(Path, "read_bytes", media)
        operation = asyncio.to_thread(store.read, session, ref.file_id)
    pending = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await case.tracker.close(session_id=session)
        record = store._catalog.get_directory(aid)
        assert record.release_pending == 1 and record.availability == "available" and directory.is_dir()
        assert store._catalog.runtime_bindings() == ()
        release.set()
        result = await asyncio.wait_for(pending, 5)
        if kind == "content":
            assert result.content == "runtime original"
        elif kind == "listing":
            assert {item.name for item in result.items} == {"original", "preview", "derived"}
        else:
            assert result.data == b"runtime original"
        record = store._catalog.get_directory(aid)
        assert record.availability == "released" and record.release_pending == 0 and not directory.exists()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_queued_reader_handoff_does_not_lose_pending_detach(access_case, monkeypatch) -> None:
    case, session, ref, aid, directory = access_case
    store = case.runner.container.attachment_blob_store
    first_read, second_read, first_done = (threading.Event() for _ in range(3))
    allow_first, allow_second = threading.Event(), threading.Event()
    state_lock = threading.Lock()
    reads = settlements = 0
    read_bytes, settle = Path.read_bytes, store._settle_directory_access

    def read(path: Path) -> bytes:
        nonlocal reads
        result = read_bytes(path)
        if path == directory / "original/content":
            with state_lock:
                reads += 1
                order = reads
            entered, release = (first_read, allow_first) if order == 1 else (second_read, allow_second)
            entered.set()
            if not release.wait(10):
                raise TimeoutError("排队读取屏障未释放")
        return result

    def settle_in_order(identity: str) -> None:
        nonlocal settlements
        with state_lock:
            settlements += 1
            order = settlements
        if order == 1:
            assert second_read.wait(5), "第一个读取收尾前，排队读取必须已接手原 holder"
        settle(identity)
        if order == 1:
            first_done.set()

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(store, "_settle_directory_access", settle_in_order)
    first = asyncio.create_task(asyncio.to_thread(store.read, session, ref.file_id))
    second = None
    try:
        assert await asyncio.to_thread(first_read.wait, 5)
        second = asyncio.create_task(asyncio.to_thread(store.read, session, ref.file_id))
        await case.tracker.close(session_id=session)
        assert store._catalog.get_directory(aid).release_pending == 1
        allow_first.set()
        assert await asyncio.to_thread(first_done.wait, 5)
        assert (await first).data == b"runtime original"
        assert directory.is_dir() and not second.done()
        allow_second.set()
        assert (await asyncio.wait_for(second, 5)).data == b"runtime original"
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
    finally:
        allow_first.set()
        allow_second.set()
        await asyncio.gather(first, *(() if second is None else (second,)), return_exceptions=True)


async def test_old_shard_collision_does_not_block_an_unrelated_directory_release(access_case, monkeypatch) -> None:
    case, _session, ref, aid, directory = access_case
    store, service = case.runner.container.attachment_blob_store, case.runner.container.workspace_service
    old_shard = zlib.crc32(aid.encode()) % KEY_LOCK_SHARDS
    collision = next(f"att_{value:032x}" for value in range(10000)
                     if zlib.crc32(f"att_{value:032x}".encode()) % KEY_LOCK_SHARDS == old_shard)
    monkeypatch.setattr(ingest_module, "create_uuid_hex", lambda *, namespace: collision.removeprefix("att_"))
    other_session, _thread, other_ref = await execute_upload(case)
    assert parse_attachment_uri(other_ref.file_id, require_file=True).attachment_id == collision and collision != aid
    other_directory = attachment_directory(store, collision)
    entered, release = threading.Event(), threading.Event()
    read = service_module.read_file_preimage

    def pause_read(*args, **kwargs):
        result = read(*args, **kwargs)
        entered.set()
        if not release.wait(10):
            raise TimeoutError("无关正文读取屏障未释放")
        return result

    monkeypatch.setattr(service_module, "read_file_preimage", pause_read)
    pending = asyncio.create_task(service.get_file_content(path=ref.file_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.wait_for(case.tracker.close(session_id=other_session), 3)
        assert store._catalog.get_directory(collision).availability == "released" and not other_directory.exists()
        assert not pending.done() and directory.is_dir()
        release.set()
        assert (await asyncio.wait_for(pending, 5)).content == "runtime original"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_restoring_observed_directories_does_not_reopen_released_working_copies(access_case) -> None:
    case, session, ref, aid, directory = access_case
    store, service = case.runner.container.attachment_blob_store, case.runner.container.workspace_service
    await service.start()
    try:
        await service.list_files(path=ref.directory_uri)
        assert service.file_operations.store.observed_directories()
        await case.tracker.close(session_id=session)
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
        references = await service.file_references([ref.file_id])
        assert references.uris == [ref.file_id]
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
        await service.stop()
        await service.start()
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
        assert (await service.list_files(path=ref.directory_uri)).items
        assert store._catalog.get_directory(aid).availability == "available" and directory.is_dir()
    finally:
        await service.stop()


async def test_last_detach_during_watch_initialization_finishes_without_body_holder(access_case, monkeypatch) -> None:
    case, session, ref, aid, directory = access_case
    container = case.runner.container
    store, service = container.attachment_blob_store, container.workspace_service
    entered, release = threading.Event(), threading.Event()
    watch_thread_id: int | None = None
    lookup, resolve = store._catalog.get_directory, store.resolve_directory_uri

    def watch_reference(*args, **kwargs):
        nonlocal watch_thread_id
        if kwargs.get("require_directory"):
            watch_thread_id = threading.get_ident()
        return resolve(*args, **kwargs)

    def pause_lookup(identity: str):
        record = lookup(identity)
        if identity == aid and threading.get_ident() == watch_thread_id and not entered.is_set():
            assert not store._directory_access_held(aid)
            entered.set()
            if not release.wait(10):
                raise TimeoutError("附件观察 locator 屏障未释放")
        return record

    monkeypatch.setattr(store, "resolve_directory_uri", watch_reference)
    monkeypatch.setattr(store._catalog, "get_directory", pause_lookup)
    assert ref.directory_uri is not None
    pending = asyncio.create_task(stream_workspace_file_events(WorkspaceFileWatchRequest(paths=[ref.directory_uri]),
        _="local", watch_service=container.workspace_file_watch_service, workspace_service=service))
    response = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.wait_for(case.tracker.close(session_id=session), 5)
        record = lookup(aid)
        assert record.availability == "released" and record.release_pending == 0 and not directory.exists()
        release.set()
        response = await asyncio.wait_for(pending, 5)
        assert lookup(aid).availability == "released" and not directory.exists()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        if response is not None:
            await cast(AsyncGenerator[str, None], response.body_iterator).aclose()


@pytest.mark.skipif(os.name == "nt", reason="验证 POSIX 原生 Shell 的目录替换")
async def test_watch_rejects_fixed_layout_path_replaced_by_native_shell_file(access_case) -> None:
    case, _session, ref, aid, directory = access_case
    container = case.runner.container
    assert ref.directory_uri is not None
    await asyncio.to_thread(subprocess.run, "rmdir derived && printf result > derived", shell=True,
        cwd=directory, check=True, capture_output=True, timeout=5)
    derived_uri = file_uri_child(ref.directory_uri, "derived")
    with pytest.raises(HTTPException) as rejected:
        await stream_workspace_file_events(WorkspaceFileWatchRequest(paths=[derived_uri]),
            _="local", watch_service=container.workspace_file_watch_service, workspace_service=container.workspace_service)
    assert rejected.value.status_code == 400
    assert isinstance(rejected.value.__cause__, NotADirectoryError)
    assert rejected.value.detail == f"文件监听路径不是目录: {derived_uri}"
    assert (directory / "derived").read_bytes() == b"result"
    assert (directory / "original/content").read_bytes() == b"runtime original"
    record = container.attachment_blob_store._catalog.get_directory(aid)
    assert record.availability == "available" and record.release_pending == 0


async def test_watch_released_attachment_uses_locator_and_only_publishes_selected_resources(access_case, monkeypatch) -> None:
    case, session, ref, aid, directory = access_case
    container = case.runner.container
    store, service, watcher = container.attachment_blob_store, container.workspace_service, container.workspace_file_watch_service
    _other_session, _other_thread, other_ref = await execute_upload(case)
    other_id = parse_attachment_uri(other_ref.file_id, require_file=True).attachment_id
    assert other_id is not None and other_id != aid and ref.directory_uri is not None
    other_directory = attachment_directory(store, other_id)
    await service.start()
    stream = None
    monkeypatch.setattr(workspace_api, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    try:
        await service.list_files(path=ref.directory_uri)
        await case.tracker.close(session_id=session)
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
        for path, status in [(ref.file_id, 400), (file_uri_child(ref.directory_uri, "derived", "missing"), 404)]:
            with pytest.raises(HTTPException) as invalid:
                await stream_workspace_file_events(WorkspaceFileWatchRequest(paths=[path]),
                    _="local", watch_service=watcher, workspace_service=service)
            assert invalid.value.status_code == status
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
        response = await stream_workspace_file_events(
            WorkspaceFileWatchRequest(paths=[ref.directory_uri, file_uri_child(ref.directory_uri, "derived")]),
            _="local", watch_service=watcher, workspace_service=service,
        )
        stream = cast(AsyncGenerator[str, None], response.body_iterator)
        assert await asyncio.wait_for(anext(stream), 5) == ": heartbeat\n\n"
        await asyncio.wait_for(watcher.wait_until_ready((service._workspace_root,)), 5)
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
        (other_directory / "derived/secret.txt").write_text("未订阅的附件结果", encoding="utf-8")
        (service._workspace_root / ".boxteam/private-watch-secret").write_text("私有状态", encoding="utf-8")
        for _ in range(8):
            assert await asyncio.wait_for(anext(stream), 5) == ": heartbeat\n\n"
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()

        assert (await service.get_file_content(path=ref.file_id)).content == "runtime original"
        changes = []
        for _ in range(100):
            frame = await asyncio.wait_for(anext(stream), 5)
            if frame.startswith(":"):
                continue
            assert frame.startswith("event: changes\ndata: "), frame
            batch = json.loads(frame.removeprefix("event: changes\ndata: "))
            changes.extend(batch["changes"])
            if changes:
                break
        assert changes and all(change["path"] == ref.directory_uri or change["path"].startswith(ref.directory_uri + "/") for change in changes)
        assert store._catalog.get_directory(aid).availability == "available" and directory.is_dir()
        assert (directory / "original/content").read_bytes() == b"runtime original"
    finally:
        if stream is not None:
            await stream.aclose()
        await service.stop()


@pytest.mark.parametrize("released_before", [False, True], ids=["detach-during-dispatch", "rebuild-before-dispatch"])
async def test_reveal_holds_original_owner_through_native_dispatch(access_case, monkeypatch, released_before: bool) -> None:
    case, session, ref, aid, directory = access_case
    store, service = case.runner.container.attachment_blob_store, case.runner.container.workspace_service
    entered, release = threading.Event(), threading.Event()
    calls = []

    def dispatch(command, **options):
        assert store._directory_access_held(aid)
        assert (directory / "original/content").read_bytes() == b"runtime original"
        calls.append((command, options))
        entered.set()
        if not release.wait(10):
            raise TimeoutError("系统定位派发屏障未释放")

    monkeypatch.setattr(service_module, "shutil", SimpleNamespace(which=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None))
    monkeypatch.setattr(service_module, "subprocess", SimpleNamespace(Popen=dispatch, DEVNULL=-3, CREATE_NEW_PROCESS_GROUP=0x200))
    if released_before:
        await case.tracker.close(session_id=session)
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
    pending = asyncio.create_task(reveal_workspace_file_entry(path=ref.file_id, scope="workspace", _="local",
        request_id="attachment-reveal", workspace_service=service))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if not released_before:
            await case.tracker.close(session_id=session)
            assert store._catalog.get_directory(aid).release_pending == 1
        assert directory.is_dir() and not pending.done()
        release.set()
        result = await asyncio.wait_for(pending, 5)
        assert result.data is not None
        assert result.data.path == ref.file_id and result.request_id == "attachment-reveal"
        assert len(calls) == 1 and any(str(directory) in argument for argument in calls[0][0])
        record = store._catalog.get_directory(aid)
        assert record.release_pending == 0
        assert record.availability == ("available" if released_before else "released")
        assert directory.exists() is released_before
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("subscribed", [True, False], ids=["selected", "private"])
async def test_replaying_original_file_operation_event_does_not_reopen_released_directory(access_case, subscribed: bool) -> None:
    case, session, ref, aid, directory = access_case
    container = case.runner.container
    store, service = container.attachment_blob_store, container.workspace_service
    assert ref.directory_uri is not None
    derived = file_uri_child(ref.directory_uri, "derived")
    await service.start()
    stream = None
    try:
        owner = service.file_operations
        initial = await service.list_files(path=derived)
        cursor = owner.store.latest_event_id()
        receipt = await service.create_file_entry(directory_path=derived, scope="workspace", name="result.txt", kind="file",
            intent=WorkspaceFileMutationIntentDTO(client_operation_id="op_" + uuid4().hex, client_sequence=1,
                directory_key=initial.directory_key, expected_directory_revision=initial.directory_revision), actor="local")
        for _ in range(300):
            result = owner.status([receipt.operation_id], actor="local").operations[0]
            if result.state == "committed":
                break
            owner.require_ready()
            await asyncio.sleep(0.01)
        else:
            pytest.fail("原文件 operation 未提交回放事件")
        assert result.effect == "complete"
        expected = next(value for value in owner.store.events(cursor) if value["operation_id"] == receipt.operation_id)
        content = await service.get_file_content(path=file_uri_child(derived, "result.txt"))
        deleted = await container.text_mutations.execute_async(PatchText(
            target=derived, input="*** Begin Patch\n*** Delete File: result.txt\n*** End Patch",
            expected_versions={"result.txt": "sha256:" + content.revision}, explanation="显式恢复初始基线后回放原文件事件",
        ))
        assert deleted["status"] == "success"
        await case.tracker.close(session_id=session)
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
        response = await stream_workspace_file_events(
            WorkspaceFileWatchRequest(paths=[derived] if subscribed else [], after_event_id=expected["event_id"] - 1),
            _="local", watch_service=container.workspace_file_watch_service, workspace_service=service,
        )
        stream = cast(AsyncGenerator[str, None], response.body_iterator)
        pending = asyncio.create_task(anext(stream))
        try:
            if not subscribed:
                await asyncio.wait_for(container.workspace_file_watch_service.wait_until_ready((service._workspace_root,)), 5)
                (service._workspace_root / "public.txt").write_text("公开目录变化", encoding="utf-8")
            frame = await asyncio.wait_for(pending, 5)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert frame.startswith("event: changes\ndata: "), frame
        event = json.loads(frame.removeprefix("event: changes\ndata: "))
        if subscribed:
            assert event["event_id"] == expected["event_id"]
            assert event["directories"] == expected["directories"]
            assert event["snapshots"] == expected["snapshots"]
        else:
            assert event["changes"] == [{"kind": "create", "path": file_uri_child(service.file_paths.workspace_uri, "public.txt")}]
            assert event["directories"] == event["snapshots"] == []
        record = store._catalog.get_directory(aid)
        assert record.availability == "released" and record.release_pending == 0 and not directory.exists()
    finally:
        if stream is not None:
            await stream.aclose()
        await service.stop()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="当前平台没有原生 FIFO")
async def test_fifo_read_keeps_api_error_when_last_detach_runs_during_access(access_case, monkeypatch) -> None:
    case, session, ref, aid, directory = access_case
    store, service = case.runner.container.attachment_blob_store, case.runner.container.workspace_service
    assert ref.directory_uri is not None
    fifo = directory / "derived/result.fifo"
    os.mkfifo(fifo)
    reference = file_uri_child(ref.directory_uri, "derived", "result.fifo")
    entered, release = threading.Event(), threading.Event()
    read = service_module.read_file_preimage

    def pause_read(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("FIFO 原生访问屏障未释放")
        return read(*args, **kwargs)

    monkeypatch.setattr(service_module, "read_file_preimage", pause_read)
    pending = asyncio.create_task(get_workspace_file_content(path=reference, scope="workspace", _="local",
        request_id="fifo-working-copy", workspace_service=service))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await case.tracker.close(session_id=session)
        assert store._catalog.get_directory(aid).release_pending == 1
        release.set()
        with pytest.raises(HTTPException) as caught:
            await asyncio.wait_for(pending, 5)
        assert caught.value.status_code == 400 and "目标不是普通文件" in str(caught.value.detail)
        assert isinstance(caught.value.__cause__, ValueError)
        record = store._catalog.get_directory(aid)
        assert record.availability == "available" and record.release_pending == 0 and fifo.exists()
        assert record.release_reason == "目录相对上传初始基线有修改，保留原位置与处理结果"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("disconnect", [False, True], ids=["complete", "disconnect"])
async def test_download_holds_original_until_asgi_send_finishes(access_case, disconnect: bool) -> None:
    case, session, ref, aid, directory = access_case
    store, service = case.runner.container.attachment_blob_store, case.runner.container.workspace_service
    response = WorkspaceFileResponse(service.prepare_file_download(path=ref.file_id), disposition="attachment")
    entered, release = asyncio.Event(), asyncio.Event()
    bodies: list[bytes] = []
    failure = OSError("测试客户端已断开")

    async def send(message):
        if message["type"] == "http.response.body":
            bodies.append(message["body"])
            entered.set()
            await release.wait()
            if disconnect:
                raise failure

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    pending = asyncio.create_task(response({"type": "http", "method": "GET", "headers": [],
        "asgi": {"spec_version": "2.4"}, "extensions": {}}, receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await case.tracker.close(session_id=session)
        record = store._catalog.get_directory(aid)
        assert record.release_pending == 1 and directory.is_dir() and not pending.done()
        release.set()
        if disconnect:
            with pytest.raises(OSError) as caught:
                await pending
            assert caught.value.__cause__ is failure and caught.value.errno == failure.errno
            assert ref.file_id in str(caught.value) and "stage=文件传输" in str(caught.value)
        else:
            await pending
        assert b"".join(bodies) == b"runtime original"
        assert store._catalog.get_directory(aid).availability == "released" and not directory.exists()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_listing_observation_does_not_wait_for_an_unrelated_attachment_holder(access_case, monkeypatch) -> None:
    case, _session, ref, _aid, _directory = access_case
    service = case.runner.container.workspace_service
    await service.start()
    owner = service.file_operations
    assert ref.directory_uri is not None
    derived = file_uri_child(ref.directory_uri, "derived")
    initial = await service.list_files(path=derived)
    staged, publish_allowed, scanning, worker_waiting = (threading.Event() for _ in range(4))
    stage, scan, observation_lock = operations_module.stage_step, service._scan_directory, owner.observation_lock
    listing_thread: int | None = None

    def pause_stage(*args, **kwargs):
        value = stage(*args, **kwargs)
        staged.set()
        if not publish_allowed.wait(10):
            raise TimeoutError("文件发布屏障未释放")
        return value

    class ObserveLock:
        def __enter__(self):
            if scanning.is_set() and threading.get_ident() != listing_thread:
                worker_waiting.set()
            return observation_lock.__enter__()

        def __exit__(self, *args):
            return observation_lock.__exit__(*args)

    def scan_during_publication(*args, **kwargs):
        nonlocal listing_thread
        if args[0] == service._workspace_root and staged.is_set():
            listing_thread = threading.get_ident()
            scanning.set()
            publish_allowed.set()
            assert worker_waiting.wait(5), "原 worker 必须持附件 holder 等待列表观察锁"
        return scan(*args, **kwargs)

    monkeypatch.setattr(operations_module, "stage_step", pause_stage)
    monkeypatch.setattr(owner, "observation_lock", ObserveLock())
    monkeypatch.setattr(service, "_scan_directory", scan_during_publication)
    listing = None
    try:
        receipt = await service.create_file_entry(directory_path=derived, scope="workspace", name="result.txt", kind="file",
            intent=WorkspaceFileMutationIntentDTO(client_operation_id="op_" + uuid4().hex, client_sequence=1,
                directory_key=initial.directory_key, expected_directory_revision=initial.directory_revision), actor="local")
        assert await asyncio.to_thread(staged.wait, 5)
        listing = asyncio.create_task(service.list_files(path=""))
        await asyncio.wait_for(asyncio.shield(listing), 2)
        for _ in range(300):
            result = owner.status([receipt.operation_id], actor="local").operations[0]
            if result.state == "committed":
                break
            owner.require_ready()
            await asyncio.sleep(0.01)
        else:
            pytest.fail("原文件 operation 未在观察锁释放后提交")
        assert result.state == "committed" and result.effect == "complete"
        assert [item.name for item in (await service.list_files(path=derived)).items] == ["result.txt"]
    finally:
        publish_allowed.set()
        if listing is not None:
            await asyncio.gather(listing, return_exceptions=True)
        await service.stop()
