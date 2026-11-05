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


@pytest.mark.parametrize("existing", [False, True])
def test_frozen_copy_reads_each_source_and_target_once(
    attachment_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    source, target = attachment_directory, tmp_path / "target"
    manifest = directory_manifest(source)
    if existing:
        copy_directory_files(source, target, *manifest)
    paths = tuple(native_io_path(root / "original/content") for root in (source, target))
    reads: list[Path] = []
    original_open = Path.open

    def track(path: Path, mode="r", *args, **kwargs):
        if mode == "rb" and path in paths:
            reads.append(path)
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", track)
        copy_directory_files(source, target, *manifest)
    assert reads.count(paths[0]) == reads.count(paths[1]) == 1
    assert directory_manifest(target) == manifest


@pytest.mark.parametrize("damage", ["source", "fresh_source", "target", "extra_file", "extra_directory"])
def test_frozen_copy_rejects_changed_content_and_extra_nodes(
    attachment_directory: Path, tmp_path: Path, damage: str,
) -> None:
    source, target = attachment_directory, tmp_path / "target"
    manifest = directory_manifest(source)
    if damage != "fresh_source":
        copy_directory_files(source, target, *manifest)
    if damage in {"source", "fresh_source", "target"}:
        root = target if damage == "target" else source
        content = root / "original/content"
        content.write_bytes(b"x" * content.stat().st_size)
    elif damage == "extra_file":
        (target / "unexpected").write_bytes(b"unexpected")
    else:
        (target / "unexpected").mkdir()
    before = (target / "original/content").read_bytes() if damage != "fresh_source" else None
    with pytest.raises(RuntimeError, match="冻结版本不一致|复制结果与冻结清单不一致"):
        copy_directory_files(source, target, *manifest)
    if before is not None:
        assert (target / "original/content").read_bytes() == before


def test_frozen_copy_rejects_source_changes_during_read(
    attachment_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = attachment_directory, tmp_path / "target"
    manifest = directory_manifest(source)
    content = native_io_path(source / "original/content")
    original_open = Path.open
    changed = False

    def mutate_during_read(path: Path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path == content and mode == "rb":
            read = stream.read

            def read_then_mutate(size=-1):
                nonlocal changed
                chunk = read(size)
                if chunk and not changed:
                    changed = True
                    with original_open(content, "ab") as writer:
                        writer.write(b"changed-during-copy")
                return chunk

            stream.read = read_then_mutate
        return stream

    monkeypatch.setattr(Path, "open", mutate_during_read)
    with pytest.raises(RuntimeError, match="冻结期间发生变化"):
        copy_directory_files(source, target, *manifest)
    assert changed


def test_frozen_copy_still_verifies_destination_after_sync(
    attachment_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = attachment_directory, tmp_path / "target"
    manifest = directory_manifest(source)
    fsync = os.fsync

    def damage_after_sync(descriptor: int) -> None:
        fsync(descriptor)
        with (target / "original/content").open("r+b") as writer:
            writer.write(b"corrupt")

    monkeypatch.setattr(os, "fsync", damage_after_sync)
    with pytest.raises(RuntimeError, match="复制结果与冻结清单不一致"):
        copy_directory_files(source, target, *manifest)
