from __future__ import annotations

import errno
import io
import os
import subprocess
from pathlib import Path

import pytest

from app.gateway.runtime.process_lifecycle import _spawn_logged_process
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


@pytest.mark.parametrize(
    "file_name",
    [
        "",
        "../outside.log",
        "nested/log.log",
        r"..\outside.log",
        r"nested\log.log",
        r"C:\outside.log",
        r"\\server\share.log",
        "log.txt",
    ],
)
def test_process_log_store_rejects_invalid_file_name(
    tmp_path: Path,
    file_name: str,
) -> None:
    store = ProcessLogStore(tmp_path / "logs")

    with pytest.raises(ValueError, match="进程日志") as failure:
        store.path_for(file_name)
    if file_name:
        assert file_name not in str(failure.value)


@pytest.mark.skipif(os.name != "posix", reason="Windows 符号链接权限模型不同")
def test_process_log_store_rejects_symbolic_link(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("outside\n", encoding="utf-8")
    (log_root / "local-backend-12345.log").symlink_to(outside)

    with pytest.raises(RuntimeError, match="符号链接") as failure:
        ProcessLogStore(log_root).open("local-backend-12345.log")
    assert str(log_root) not in str(failure.value)
    assert outside.read_text() == "outside\n"


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
        raise PermissionError(errno.EACCES, "测试权限更新失败", str(tmp_path / "logs/gateway.log"))

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fchmod", fail_fchmod)

    with pytest.raises(PermissionError, match="stage=file_permissions") as failure:
        ProcessLogStore(tmp_path / "logs").open("gateway.log")
    assert failure.value.errno == errno.EACCES
    assert str(tmp_path) not in str(failure.value)
    assert "测试权限更新失败" in str(failure.value.__cause__)

    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])


@pytest.mark.parametrize("stage", ["create_directory", "rotate", "open"])
def test_log_filesystem_failure_keeps_location_in_cause_and_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, stage: str,
) -> None:
    log_root = tmp_path / "private-log-root"
    if stage == "create_directory":
        log_root.write_text("obstruction")
    else:
        log_root.mkdir()
        if stage == "open":
            (log_root / "gateway.log").mkdir()
        else:
            monkeypatch.setattr("app.gateway.runtime.process_logs.PROCESS_LOG_MAX_BYTES", 1)
            (log_root / "gateway.log").write_text("old")
            (log_root / "gateway.log.2").write_text("previous")
            (log_root / "gateway.log.3").mkdir()
    with pytest.raises(OSError, match=f"stage={stage}") as failure:
        ProcessLogStore(log_root).open("gateway.log")
    original = failure.value.__cause__
    assert isinstance(original, OSError)
    assert type(failure.value) is type(original)
    assert failure.value.errno == original.errno
    assert failure.value.filename is None
    assert str(log_root) not in str(failure.value)
    assert str(log_root) in str(original)
    assert str(log_root) in caplog.text
    if stage == "rotate":
        assert (log_root / "gateway.log").read_text() == "old"


@pytest.mark.parametrize("marker", ["/private/runtime/node", r"C:\private\node.exe", r"\\server\private\node.exe"])
@pytest.mark.parametrize("with_filename", [False, True])
def test_spawn_logged_process_closes_log_and_preserves_native_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, marker: str, with_filename: bool,
) -> None:
    log_file = io.StringIO()
    original = (
        PermissionError(errno.EACCES, "access denied", marker)
        if with_filename else PermissionError(errno.EACCES, f"access denied: {marker}")
    )

    def fail_spawn(*_: object, **__: object) -> subprocess.Popen[str]:
        raise original

    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.subprocess.Popen",
        fail_spawn,
    )

    with pytest.raises(PermissionError, match="stage=spawn") as failure:
        _spawn_logged_process([marker, "credential-argument-marker"], log_file=log_file)

    assert log_file.closed is True
    assert failure.value.errno == errno.EACCES
    assert failure.value.filename is None
    assert failure.value.__cause__ is original
    assert marker not in str(failure.value)
    assert str(original) in caplog.text
    assert "credential-argument-marker" not in caplog.text
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)


@pytest.mark.parametrize("system_error", [False, True])
def test_log_close_failure_does_not_replace_startup_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, system_error: bool,
) -> None:
    original = PermissionError(errno.EACCES, "native startup error", "/private/runtime/node") if system_error else ValueError("invalid environment")
    cleanup_error = OSError(errno.EIO, "native close error", "/private/runtime/log")

    class FailingLog(io.StringIO):
        def close(self) -> None:
            super().close()
            raise cleanup_error

    def fail_spawn(*_: object, **__: object) -> subprocess.Popen[str]:
        raise original

    monkeypatch.setattr("app.gateway.runtime.process_lifecycle.subprocess.Popen", fail_spawn)
    log_file = FailingLog()
    with pytest.raises(type(original)) as failure:
        _spawn_logged_process(["configured-node"], log_file=log_file)
    if system_error:
        assert failure.value.__cause__ is original
        assert failure.value.errno == errno.EACCES
    else:
        assert failure.value is original
    assert log_file.closed
    assert "/private/runtime" not in str(failure.value)
    assert any("error_type=OSError, errno=5" in note for note in failure.value.__notes__)
    assert all("/private/runtime" not in note for note in failure.value.__notes__)
    assert any(record.exc_info and record.exc_info[1] is cleanup_error for record in caplog.records)
