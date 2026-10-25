"""冻结产物导出不得将扫描失败当作空目录，也不得返回原生位置。"""

from __future__ import annotations

import base64
import errno
import hashlib
import os
from pathlib import Path

import pytest

from app.schemas.internal_v2.session_fork_transfer import (
    FORK_TRANSFER_CHUNK_BYTES,
    FORK_TRANSFER_FILE_BATCH,
)
from app.services.infrastructure.rollout_context.fork import transfer
from tests.unit.services.infrastructure.rollout_context.test_capture_io_errors import (
    CaptureFiles,
    capture_files,
    frozen,
)

__all__ = ["capture_files", "frozen"]


@pytest.mark.parametrize("phase", ["root_scan", "nested_scan", "metadata", "chunk_open"])
def test_export_native_failure_keeps_cause_bytes_and_retry(
    frozen: CaptureFiles, phase: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    item = frozen
    canonical = item.snapshot.captured_root / "rollout/rollout.jsonl"
    original = item.bytes()
    path = item.root if phase == "root_scan" else canonical.parent if phase == "nested_scan" else canonical
    owner, operation = (os, "scandir") if phase.endswith("scan") else (Path, "open" if phase == "chunk_open" else "stat")
    real = getattr(owner, operation)
    native = PermissionError(errno.EACCES, f"原生导出拒绝 {path}", str(path))
    name = canonical.relative_to(item.root).as_posix()

    def denied(value, *args, **kwargs):
        if not isinstance(value, int) and Path(value) == path:
            raise native
        return real(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner, operation, denied)
        with pytest.raises(PermissionError) as caught:
            if phase == "chunk_open":
                transfer.export_file_chunk(item.sessions, item.record, name, 0)
            else:
                transfer.export_file_batch(item.sessions, item.record, 0)
    assert caught.value.__cause__ is native and caught.value.errno == errno.EACCES
    assert caught.value.filename is None and str(item.root) not in str(caught.value)
    stage = "export_chunk" if phase == "chunk_open" else "export_manifest"
    assert "artifact=fork_transfer stage=" + stage in str(caught.value)
    assert str(path) in caplog.text and item.bytes() == original
    assert transfer.export_file_batch(item.sessions, item.record, 0).files
    assert base64.b64decode(transfer.export_file_chunk(item.sessions, item.record, name, 0).chunk_base64) == original[name]


@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_export_rejects_symbolic_links_without_reading_target(
    frozen: CaptureFiles, tmp_path: Path, directory: bool,
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"outside untouched")
    link = frozen.snapshot.captured_root / "rollout/link"
    link.symlink_to(target if directory else sentinel, target_is_directory=directory)
    with pytest.raises(RuntimeError, match="符号链接"):
        transfer.export_file_batch(frozen.sessions, frozen.record, 0)
    assert sentinel.read_bytes() == b"outside untouched" and link.is_symlink()


def test_export_pages_and_chunks_keep_frozen_identity_order_and_bytes(frozen: CaptureFiles) -> None:
    directory = frozen.snapshot.captured_root / "rollout/parts"
    directory.mkdir()
    for index in range(FORK_TRANSFER_FILE_BATCH + 5):
        (directory / f"part-{index:03}.bin").write_bytes(str(index).encode())
    large = directory / "large.bin"
    payload = bytes(range(256)) * (FORK_TRANSFER_CHUNK_BYTES // 256 + 1)
    large.write_bytes(payload)
    expected = frozen.bytes()
    cursor, names = 0, []
    while True:
        page = transfer.export_file_batch(frozen.sessions, frozen.record, cursor)
        assert page == transfer.export_file_batch(frozen.sessions, frozen.record, cursor)
        assert page.capture.to_record() == frozen.record
        assert len(page.files) <= FORK_TRANSFER_FILE_BATCH
        names.extend(file.name for file in page.files)
        assert all(file.size_bytes == len(expected[file.name]) for file in page.files)
        if page.next_cursor is None:
            break
        assert page.next_cursor == cursor + FORK_TRANSFER_FILE_BATCH
        cursor = page.next_cursor
    assert len(names) == len(set(names)) and set(names) == set(expected)
    name = large.relative_to(frozen.root).as_posix()
    output = b""
    while len(output) < len(payload):
        reply = transfer.export_file_chunk(frozen.sessions, frozen.record, name, len(output))
        block = base64.b64decode(reply.chunk_base64, validate=True)
        assert reply.capture.to_record() == frozen.record and reply.offset == len(output)
        assert 0 < len(block) <= FORK_TRANSFER_CHUNK_BYTES
        assert reply.chunk_sha256 == hashlib.sha256(block).hexdigest()
        output += block
    assert output == payload and frozen.bytes() == expected
