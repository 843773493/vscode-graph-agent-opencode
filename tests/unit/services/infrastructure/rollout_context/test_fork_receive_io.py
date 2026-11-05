"""接收文件的本机失败与端间传输失败保留各自原始归属。"""

from __future__ import annotations

import asyncio
import errno
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.resource_uri import resource_display_uri, thread_resource_uri
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.internal_v2.session import SessionForkRequest
from app.services.infrastructure.rollout_context.fork import source_capture, transfer
from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    _tree_manifest,
)
from tests.unit.services.infrastructure.rollout_context.test_capture_io_errors import (
    capture_files,
    frozen,
)

__all__ = ["capture_files", "frozen"]


@pytest.fixture
def transfer_request(capture_files):
    return SessionForkRequest(idempotency_key="receive-test", source_view_id="view-test", source_view_revision=0,
        creation_kind="new_session_main", mode="full_rollout_copy",
        source_uri=thread_resource_uri("workspace-source", capture_files.record.source_session_id,
            capture_files.record.source_thread_id, gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729"),
        target_container_uri=resource_display_uri(scope="workspace", scope_id="workspace-target", kind="session",
            tail_segments=(), gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744"))


@pytest.fixture
def receive_case(frozen, tmp_path, transfer_request):
    name = frozen.record.source_session_id + "/rollout/rollout.jsonl"
    request = transfer_request
    calls = []

    async def send(payload):
        assert payload.action == "chunk" and payload.file_name == name
        calls.append(payload)
        return transfer.export_file_chunk(frozen.sessions, frozen.record, payload.file_name, payload.offset)

    path = tmp_path / "received/subdirectory/output.jsonl"
    transport = SimpleNamespace(fork_transfer=send)
    original = (frozen.root / name).read_bytes()

    async def receive():
        await transfer.receive_transfer_file(transport, request, frozen.record, path=path,
            size_bytes=len(original), file_name=name)

    token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    try:
        yield SimpleNamespace(request=request, path=path, original=original, source=frozen.root / name,
                              receive=receive, transport=transport, calls=calls)
    finally:
        reset_current_gateway_id(token)


@pytest.mark.parametrize("phase", ["mkdir", "open", "write", "flush", "fsync", "close", "publish", "combined"])
async def test_receive_native_failure_keeps_type_cause_and_source(receive_case, monkeypatch, caplog, phase):
    case = receive_case
    path = case.path
    native = PermissionError(errno.EACCES, f"原生接收拒绝 {path}", str(path))
    close_error = PermissionError(errno.EACCES, f"原生关闭拒绝 {path}", str(path))
    real_open, real_mkdir, real_fsync, real_publish = Path.open, Path.mkdir, transfer.os.fsync, transfer.fsync_directory
    opened = []

    def fail_open(value, *args, **kwargs):
        if value != path:
            return real_open(value, *args, **kwargs)
        if phase == "open":
            raise native
        stream = real_open(value, *args, **kwargs)
        opened.append(stream)
        writer = Mock(wraps=stream)
        writer.__enter__ = Mock(return_value=writer)
        writer.__exit__ = Mock(side_effect=lambda *_: writer.close())
        if phase in {"write", "combined"}:
            writer.write.side_effect = native
        if phase == "flush":
            writer.flush.side_effect = native
        if phase in {"close", "combined"}:
            def fail_close():
                stream.close()
                raise close_error if phase == "combined" else native
            writer.close.side_effect = fail_close
        return writer

    def fail_mkdir(value, *args, **kwargs):
        if value == path.parent:
            raise native
        return real_mkdir(value, *args, **kwargs)

    def fail_fsync(descriptor):
        if opened and not opened[0].closed and descriptor == opened[0].fileno():
            raise native
        return real_fsync(descriptor)

    def fail_publish(value):
        if value == path.parent:
            raise native
        return real_publish(value)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_open)
        if phase == "mkdir":
            patch.setattr(Path, "mkdir", fail_mkdir)
        if phase == "fsync":
            patch.setattr(transfer.os, "fsync", fail_fsync)
        if phase == "publish":
            patch.setattr(transfer, "fsync_directory", fail_publish)
        with pytest.raises(PermissionError) as caught:
            await case.receive()
    expected = close_error if phase == "combined" else native
    stage = {"mkdir": "receive_open", "open": "receive_open", "write": "receive_write", "flush": "receive_flush",
             "fsync": "receive_flush", "close": "receive_close", "combined": "receive_close", "publish": "receive_publish"}[phase]
    assert caught.value.__cause__ is expected and caught.value.errno == errno.EACCES
    assert caught.value.filename is None and str(path) not in str(caught.value)
    assert "artifact=fork_transfer stage=" + stage in str(caught.value) and str(path) in caplog.text
    if phase == "combined":
        assert close_error.__context__.__cause__ is native
        assert "stage=receive_write" in caplog.text
    assert all(stream.closed for stream in opened) and case.source.read_bytes() == case.original
    path.unlink(missing_ok=True)
    await case.receive()
    assert path.read_bytes() == case.original and case.source.read_bytes() == case.original


async def test_transport_error_is_not_relabelled_as_local_file_io(receive_case, monkeypatch, caplog):
    case = receive_case
    upstream = ConnectionError(errno.ECONNRESET, "来源传输连接中断")
    opened = []
    real_open = Path.open

    def record_open(value, *args, **kwargs):
        stream = real_open(value, *args, **kwargs)
        if value == case.path:
            opened.append(stream)
        return stream

    async def failed_transfer(_payload):
        raise upstream

    monkeypatch.setattr(Path, "open", record_open)
    monkeypatch.setattr(case.transport, "fork_transfer", failed_transfer)
    with pytest.raises(ConnectionError) as caught:
        await case.receive()
    assert caught.value is upstream and upstream.__cause__ is None
    assert opened and all(stream.closed for stream in opened)
    assert "artifact=fork_transfer" not in caplog.text
    assert case.source.read_bytes() == case.original


async def test_import_cleanup_failure_keeps_native_cause_and_original_transport(
    receive_case, frozen, tmp_path, monkeypatch,
):
    sessions = tmp_path / "target/sessions"
    root = transfer.capture_directory(sessions, frozen.record)
    root.mkdir(parents=True)
    partial = root / "partial"
    partial.write_bytes(b"unpublished transfer")
    upstream = ConnectionError("来源传输失败")
    native = PermissionError(errno.EACCES, "快照清理失败", str(root))
    original_remove = source_capture.shutil.rmtree

    async def failed_transfer(_payload):
        raise upstream

    def failed_remove(path, *args, **kwargs):
        if Path(path) == root:
            raise native
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(receive_case.transport, "fork_transfer", failed_transfer)
    monkeypatch.setattr(source_capture.shutil, "rmtree", failed_remove)
    with pytest.raises(PermissionError) as caught:
        await transfer.import_captured_artifact(receive_case.transport, receive_case.request, sessions, frozen.record)
    assert caught.value.__cause__ is native and caught.value.errno == errno.EACCES
    assert native.__context__ is upstream
    assert partial.read_bytes() == b"unpublished transfer"
    assert receive_case.source.read_bytes() == receive_case.original


@pytest.fixture
def import_case(capture_files, transfer_request, tmp_path):
    source = capture_files
    rollout = source.snapshot.captured_root / "rollout"
    for index in range(9):
        payload = b"large" * transfer.FORK_TRANSFER_CHUNK_BYTES if index == 0 else f"part-{index}".encode()
        (rollout / f"part-{index:02}.bin").write_bytes(payload)
    snapshot = replace(source.snapshot, rollout=replace(source.snapshot.rollout, files=_tree_manifest(rollout)))
    marker = source_capture.publish_captured_snapshot(source.record, snapshot)
    record = replace(source.record, state="source_captured", marker_sha256=marker,
        snapshot_sha256=snapshot.snapshot_sha256, source_point_json=snapshot.source_point.frozen_json(
            preimage_hash=source.record.preimage_hash, snapshot_sha256=snapshot.snapshot_sha256, marker_sha256=marker))
    token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    try:
        yield SimpleNamespace(source=source, record=record, snapshot=snapshot, request=transfer_request,
                              target=tmp_path / "target/sessions", original=source.bytes())
    finally:
        reset_current_gateway_id(token)


@pytest.mark.parametrize("fail", [False, True], ids=["drain-page", "cancel-writers"])
async def test_import_keeps_workers_busy_and_settles_them_before_publication_or_cleanup(import_case, fail, monkeypatch):
    case = import_case
    # 用真正需要分块的文件保留 worker 调度/取消证明；小文件另验批内复用。
    for index in range(1, 9):
        (case.source.snapshot.captured_root / f"rollout/part-{index:02}.bin").write_bytes(
            bytes([index]) * (transfer.FORK_TRANSFER_CHUNK_BYTES + 1),
        )
    snapshot = replace(case.snapshot, rollout=replace(case.snapshot.rollout,
        files=_tree_manifest(case.source.snapshot.captured_root / "rollout")))
    marker = source_capture.publish_captured_snapshot(case.record, snapshot)
    case.record = replace(case.record, snapshot_sha256=snapshot.snapshot_sha256, marker_sha256=marker,
        source_point_json=snapshot.source_point.frozen_json(preimage_hash=case.record.preimage_hash,
            snapshot_sha256=snapshot.snapshot_sha256, marker_sha256=marker))
    case.snapshot, case.original = snapshot, case.source.bytes()
    last_file = asyncio.Event()
    large_waiting = asyncio.Event()
    active = peak = 0
    completed = set()
    cancelled = []
    failure = ConnectionError("源分块失败")
    real_flush = transfer._flush_received_file

    def record_flush(path, stream):
        name = path.relative_to(transfer.capture_directory(case.target, case.record)).as_posix()
        if name == "captured.json":
            assert completed == set(case.original) - {"captured.json"} and active == 0
        real_flush(path, stream)
        completed.add(name)

    monkeypatch.setattr(transfer, "_flush_received_file", record_flush)

    async def send(payload):
        nonlocal active, peak
        if payload.action == "capture":
            return transfer.SessionForkTransferResultDTO(capture=transfer.ForkSourceCaptureDTO.from_record(case.record))
        if payload.action == "files":
            return transfer.export_file_batch(case.source.sessions, case.record, payload.cursor)
        assert payload.action == "chunk"
        active += 1
        peak = max(peak, active)
        try:
            if payload.file_name.endswith("part-00.bin") and payload.offset == 0:
                large_waiting.set()
                await last_file.wait()
            if payload.file_name.endswith("part-08.bin"):
                await large_waiting.wait()
                if fail:
                    raise failure
                last_file.set()
            reply = transfer.export_file_chunk(case.source.sessions, case.record, payload.file_name, payload.offset)
            assert reply.chunk_base64 is not None
            return reply
        except asyncio.CancelledError:
            cancelled.append(payload.file_name)
            raise
        finally:
            active -= 1

    class Transport:
        async def fork_transfer(self, request: transfer.SessionForkTransferRequest) -> transfer.SessionForkTransferResultDTO:
            return await send(request)

    operation = transfer.import_captured_artifact(Transport(), case.request, case.target, case.record)
    if fail:
        with pytest.raises(ConnectionError) as caught:
            await asyncio.wait_for(operation, 5)
        assert caught.value is failure
        assert any(name.endswith("part-00.bin") for name in cancelled)
        assert not transfer.capture_directory(case.target, case.record).exists()
    else:
        received, marker = await asyncio.wait_for(operation, 5)
        root = transfer.capture_directory(case.target, case.record)
        assert {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()} == case.original
        assert received.source_point == case.snapshot.source_point
        assert received.snapshot_sha256 == case.snapshot.snapshot_sha256 and marker == case.record.marker_sha256
    assert active == 0 and 1 < peak <= 4 and case.source.bytes() == case.original


@pytest.mark.parametrize("corrupt", [False, True], ids=["shared-page", "corrupt-inline"])
async def test_import_small_files_share_pages_and_keep_final_frozen_verification(import_case, corrupt):
    case = import_case
    calls = []

    class Transport:
        async def fork_transfer(self, request: transfer.SessionForkTransferRequest) -> transfer.SessionForkTransferResultDTO:
            calls.append(request)
            if request.action == "capture":
                return transfer.SessionForkTransferResultDTO(capture=transfer.ForkSourceCaptureDTO.from_record(case.record))
            if request.action == "files":
                reply = transfer.export_file_batch(case.source.sessions, case.record, request.cursor)
                if corrupt:
                    files = [file.model_copy(update={"content_base64": transfer.base64.b64encode(
                        b"x" * file.size_bytes).decode("ascii")}) if file.name.endswith("part-01.bin") else file
                        for file in reply.files]
                    reply = reply.model_copy(update={"files": files})
                return reply
            assert request.action == "chunk" and request.file_name is not None
            assert len(case.original[request.file_name]) > transfer.FORK_TRANSFER_CHUNK_BYTES
            return transfer.export_file_chunk(case.source.sessions, case.record, request.file_name, request.offset)

    operation = transfer.import_captured_artifact(Transport(), case.request, case.target, case.record)
    if corrupt:
        with pytest.raises(RuntimeError, match="files hash/length"):
            await operation
        assert not transfer.capture_directory(case.target, case.record).exists()
    else:
        received, marker = await operation
        root = transfer.capture_directory(case.target, case.record)
        assert {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()} == case.original
        assert received.snapshot_sha256 == case.snapshot.snapshot_sha256 and marker == case.record.marker_sha256
    assert sum(request.action == "chunk" for request in calls) == sum(
        (len(content) + transfer.FORK_TRANSFER_CHUNK_BYTES - 1) // transfer.FORK_TRANSFER_CHUNK_BYTES
        for content in case.original.values() if len(content) > transfer.FORK_TRANSFER_CHUNK_BYTES
    )
    assert len(calls) < len(case.original)
    assert case.source.bytes() == case.original
