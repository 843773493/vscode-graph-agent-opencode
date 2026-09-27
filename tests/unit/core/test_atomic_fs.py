"""app/core/atomic_fs.py 崩溃安全语义测试。

守护真正重要的语义：中途失败不留半截目标文件、临时文件不残留、
文件 fsync 先于 os.replace、目录项 fsync 在 replace 之后。
这些用例可杀变异（例如把 os.replace 换成直接写、删除目录 fsync）。
只使用 tmp_path，不触碰真实工作区。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import atomic_fs


def test_atomic_write_replaces_target_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "session.json"
    target.write_bytes(b"old")
    atomic_fs.atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
    # 临时文件（前缀 .<name>.）不得残留。
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "session.json"]
    assert leftovers == []


def test_atomic_write_failure_keeps_original_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """写入中途失败：目标保持旧内容，临时文件被清理，异常向上传播。"""
    target = tmp_path / "session.json"
    target.write_bytes(b"old")

    def failing_fsync(fd: int) -> None:
        raise OSError("模拟写盘失败")

    monkeypatch.setattr(atomic_fs.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="模拟写盘失败"):
        atomic_fs.atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"old"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "session.json"]
    assert leftovers == []


def test_atomic_write_fsyncs_file_then_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """顺序契约：文件 fsync 先于 os.replace，目录 fsync 在 replace 之后。"""
    events: list[str] = []
    target = tmp_path / "session.json"

    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def recording_replace(src: object, dst: object) -> None:
        events.append("replace")
        real_replace(src, dst)  # type: ignore[arg-type]

    def recording_dir_fsync(directory: Path) -> None:
        events.append("dir_fsync")

    monkeypatch.setattr(atomic_fs.os, "fsync", recording_fsync)
    monkeypatch.setattr(atomic_fs.os, "replace", recording_replace)
    monkeypatch.setattr(atomic_fs, "fsync_directory", recording_dir_fsync)

    atomic_fs.atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"new"
    assert events == ["fsync", "replace", "dir_fsync"]


def test_atomic_write_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "session.json"
    atomic_fs.atomic_write_bytes(target, b"payload")
    assert target.read_bytes() == b"payload"


def test_fsync_directory_observable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fsync_directory 必须真的对目录描述符调用 os.fsync。"""
    calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(atomic_fs.os, "fsync", recording_fsync)
    atomic_fs.fsync_directory(tmp_path)
    assert len(calls) == 1


def test_fsync_file_observable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "control.sqlite"
    path.write_bytes(b"x")
    calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(atomic_fs.os, "fsync", recording_fsync)
    atomic_fs.fsync_file(path)
    assert len(calls) == 1
