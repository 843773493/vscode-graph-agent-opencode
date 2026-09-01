from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest

from app.gateway.runtime.process import _spawn_logged_process
from app.gateway.runtime.process_logs import (
    PROCESS_LOG_MAX_BYTES,
    ProcessLogStore,
)


def test_process_log_store_appends_and_restricts_permissions(tmp_path: Path) -> None:
    log_root = tmp_path / "gateway" / "logs"
    store = ProcessLogStore(log_root)

    with store.open("local-backend-12345.log") as stream:
        stream.write("first\n")
    with store.open("local-backend-12345.log") as stream:
        stream.write("second\n")

    log_path = log_root / "local-backend-12345.log"
    assert log_path.read_text(encoding="utf-8") == "first\nsecond\n"
    if os.name == "posix":
        assert log_root.stat().st_mode & 0o777 == 0o700
        assert log_path.stat().st_mode & 0o777 == 0o600


def test_process_log_store_rotates_an_oversized_previous_log(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    log_path = log_root / "local-backend-12345.log"
    log_path.write_bytes(b"x" * PROCESS_LOG_MAX_BYTES)

    with ProcessLogStore(log_root).open(log_path.name) as stream:
        stream.write("new\n")

    assert log_path.read_text(encoding="utf-8") == "new\n"
    assert (
        log_root / "local-backend-12345.log.1"
    ).stat().st_size == PROCESS_LOG_MAX_BYTES


@pytest.mark.parametrize("file_name", ["", "../outside.log", "nested/log.log", "log.txt"])
def test_process_log_store_rejects_invalid_file_name(
    tmp_path: Path,
    file_name: str,
) -> None:
    store = ProcessLogStore(tmp_path / "logs")

    with pytest.raises(ValueError, match="进程日志"):
        store.path_for(file_name)


@pytest.mark.skipif(os.name != "posix", reason="Windows 符号链接权限模型不同")
def test_process_log_store_rejects_symbolic_link(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("outside\n", encoding="utf-8")
    (log_root / "local-backend-12345.log").symlink_to(outside)

    with pytest.raises(RuntimeError, match="符号链接"):
        ProcessLogStore(log_root).open("local-backend-12345.log")


@pytest.mark.skipif(os.name != "posix", reason="Windows 不使用 fchmod")
def test_process_log_store_closes_descriptor_when_permission_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = os.open
    opened_descriptors: list[int] = []

    def tracked_open(path: Path, flags: int, mode: int) -> int:
        descriptor = real_open(path, flags, mode)
        opened_descriptors.append(descriptor)
        return descriptor

    def fail_fchmod(_: int, __: int) -> None:
        raise PermissionError("测试权限更新失败")

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fchmod", fail_fchmod)

    with pytest.raises(PermissionError, match="测试权限更新失败"):
        ProcessLogStore(tmp_path / "logs").open("gateway.log")

    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])


def test_spawn_logged_process_closes_log_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_file = io.StringIO()

    def fail_spawn(*_: object, **__: object) -> subprocess.Popen[str]:
        raise OSError("测试进程创建失败")

    monkeypatch.setattr(
        "app.gateway.runtime.process.subprocess.Popen",
        fail_spawn,
    )

    with pytest.raises(OSError, match="测试进程创建失败"):
        _spawn_logged_process(["missing-command"], log_file=log_file)

    assert log_file.closed is True
