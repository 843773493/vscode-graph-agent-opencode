"""原件保护在真实文件系统上保持只读、可重试且不改变内容。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.core.atomic_fs import native_io_path
from app.services.infrastructure.attachment_blob_catalog.directories import (
    copy_directory_files,
    directory_manifest,
    protect_original,
    remove_directory,
)


@pytest.fixture
def attachment_directory(tmp_path: Path):
    root = tmp_path / "attachment"
    (root / "original").mkdir(parents=True)
    (root / "original/content").write_bytes(b"immutable-original\x00\xff")
    yield root
    remove_directory(root)


def test_original_protection_is_durable_and_repeatable(attachment_directory: Path) -> None:
    original = attachment_directory / "original/content"
    before = original.read_bytes()
    for _ in range(2):
        protect_original(attachment_directory)
        assert original.read_bytes() == before
        assert not stat.S_IMODE(original.stat().st_mode) & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    # Windows 对所有调用者强制只读位；POSIX 的 root 不受 mode 写位限制。
    if os.name == "nt":
        with pytest.raises(PermissionError):
            original.open("r+b")


def test_original_sync_failure_is_not_reported_as_success(
    attachment_directory: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = attachment_directory / "original/content"
    failure = OSError("original durability barrier failed")

    def fail_sync(descriptor: int) -> None:
        assert os.fstat(descriptor).st_size == len(b"immutable-original\x00\xff")
        raise failure

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError) as raised:
        protect_original(attachment_directory)
    assert raised.value is failure
    assert original.read_bytes() == b"immutable-original\x00\xff"
    # 即使同步失败，原件也保持保护；句柄已退出，随后可显式清理。
    assert not original.stat().st_mode & stat.S_IWUSR
    monkeypatch.undo()
    protect_original(attachment_directory)
    assert original.read_bytes() == b"immutable-original\x00\xff"
    assert not original.stat().st_mode & stat.S_IWUSR


def test_long_capture_preserves_manifest_protection_and_cleanup(tmp_path: Path) -> None:
    """保持普通绝对路径输入，在原生 I/O 内处理超过 MAX_PATH 的 capture。"""
    source = tmp_path / "source"
    target = tmp_path / ("capture-" + "c" * 80) / ("frozen-" + "f" * 80) / "附件"
    assert len(str(target / "original/content.bin")) > 260
    (native_io_path(source) / "original").mkdir(parents=True)
    (native_io_path(source) / "derived/空目录").mkdir(parents=True)
    body = b"immutable-original\x00\xff"
    (native_io_path(source) / "original/content.bin").write_bytes(body)
    manifest = directory_manifest(source)
    try:
        for _ in range(2):
            copy_directory_files(source, target, *manifest)
            assert directory_manifest(target) == manifest
            assert (native_io_path(target) / "original/content.bin").read_bytes() == body
        protect_original(target)
        assert not (native_io_path(target) / "original/content.bin").stat().st_mode & stat.S_IWUSR
        remove_directory(target)
        assert not native_io_path(target).exists()
        assert directory_manifest(source) == manifest
    finally:
        remove_directory(source)
        remove_directory(target)
