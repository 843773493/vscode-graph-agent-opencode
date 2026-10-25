"""接收文件的本机失败与端间传输失败保留各自原始归属。"""

from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.resource_uri import resource_display_uri, thread_resource_uri
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.internal_v2.session import SessionForkRequest
from app.services.infrastructure.rollout_context.fork import transfer
from tests.unit.services.infrastructure.rollout_context.test_capture_io_errors import (
    capture_files,
    frozen,
)

__all__ = ["capture_files", "frozen"]


@pytest.fixture
def receive_case(frozen, tmp_path):
    name = frozen.record.source_session_id + "/rollout/rollout.jsonl"
    request = SessionForkRequest(idempotency_key="receive-test", source_view_id="view-test", source_view_revision=0,
        creation_kind="new_session_main", mode="full_rollout_copy",
        source_uri=thread_resource_uri("workspace-source", frozen.record.source_session_id,
            frozen.record.source_thread_id, gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729"),
        target_container_uri=resource_display_uri(scope="workspace", scope_id="workspace-target", kind="session",
            tail_segments=(), gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744"))
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
        yield SimpleNamespace(path=path, original=original, source=frozen.root / name,
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
