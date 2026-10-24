"""原件保护在真实文件系统上保持只读、可重试且不改变内容。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.services.infrastructure.attachment_blob_catalog.directories import (
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
