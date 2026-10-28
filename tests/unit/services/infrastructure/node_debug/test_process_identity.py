from __future__ import annotations

import logging
import os
from pathlib import Path

import psutil
import pytest

from app.services.infrastructure.node_debug.process import process_identity
from app.services.infrastructure.node_debug.process.process_identity import (
    IDENTITY_SOURCE_LINUX_PROC,
    IDENTITY_SOURCE_PSUTIL,
    probe_process_identity,
)


def test_psutil_provides_verifiable_identity_off_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)

    identity = probe_process_identity(os.getpid())

    assert identity is not None
    assert identity.source == IDENTITY_SOURCE_PSUTIL
    assert identity.verifiable
    assert identity.compare(
        recorded_source=identity.source,
        recorded_start_marker=identity.start_marker,
    ) == "match"
    assert identity.compare(
        recorded_source=identity.source,
        recorded_start_marker=f"{identity.start_marker}-reused",
    ) == "mismatch"


def test_linux_missing_proc_entry_is_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda _path: pytest.fail("Linux PID 身份探测应直接读取 /proc/stat"),
    )
    monkeypatch.setattr(
        process_identity,
        "_probe_psutil_identity",
        lambda _pid: pytest.fail("Linux /proc 读取结果不得回落到 psutil"),
    )

    assert probe_process_identity(4242) is None


def test_linux_process_identity_requires_boot_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda _path: pytest.fail("Linux 进程身份读取不应先探测文件是否存在"),
    )
    process_root = tmp_path / "4242"
    process_root.mkdir()
    stat_fields = ["S", *(["0"] * 18), "12345"]
    (process_root / "stat").write_text(
        f"4242 (python) {' '.join(stat_fields)}",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="boot_id"):
        probe_process_identity(4242)

    boot_id_path = tmp_path / "sys" / "kernel" / "random" / "boot_id"
    boot_id_path.parent.mkdir(parents=True)
    boot_id_path.write_text("\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger=process_identity.__name__), pytest.raises(RuntimeError, match="启动 ID 为空") as caught:
        probe_process_identity(4242)
    assert str(boot_id_path) not in str(caught.value)
    assert f"source={IDENTITY_SOURCE_LINUX_PROC}" in str(caught.value)
    assert str(boot_id_path) in caplog.text


@pytest.mark.parametrize("raw,reason", [
    ("进程身份原文 /private/identity-secret", "格式异常"),
    ("4242 (/private/identity-secret) S 0", "字段不足"),
])
def test_linux_malformed_identity_rejects_without_publishing_private_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    raw: str, reason: str,
) -> None:
    process_root = tmp_path / "4242"
    process_root.mkdir()
    stat_path = process_root / "stat"
    stat_path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_probe_psutil_identity", lambda _pid: pytest.fail("损坏的 /proc 身份不得回落"))

    with caplog.at_level(logging.ERROR, logger=process_identity.__name__), pytest.raises(RuntimeError, match=reason) as caught:
        probe_process_identity(4242)
    message = str(caught.value)
    assert str(tmp_path) not in message and "identity-secret" not in message and "/proc/" not in message
    assert "pid=4242" in message and f"source={IDENTITY_SOURCE_LINUX_PROC}" in message
    assert str(stat_path) in caplog.text and raw in caplog.text


@pytest.fixture(params=["/private/identity-secret", r"C:\private\identity-secret", r"\\server\private\identity-secret"])
def psutil_identity_error(request: pytest.FixtureRequest) -> psutil.AccessDenied:
    return psutil.AccessDenied(pid=4242, name=request.param, msg=f"读取失败: {request.param}")


def test_psutil_identity_failure_keeps_cause_and_private_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    psutil_identity_error: psutil.AccessDenied,
) -> None:
    def denied_process(pid: int) -> None:
        assert pid == 4242
        raise psutil_identity_error

    monkeypatch.setattr(psutil, "Process", denied_process)
    monkeypatch.setattr(process_identity.sys, "platform", "darwin")
    with caplog.at_level(logging.ERROR, logger=process_identity.__name__), pytest.raises(RuntimeError) as caught:
        probe_process_identity(4242)
    message = str(caught.value)
    assert caught.value.__cause__ is psutil_identity_error
    assert "identity-secret" not in message
    assert "pid=4242" in message and f"source={IDENTITY_SOURCE_PSUTIL}" in message
    assert "error_type=AccessDenied" in message
    assert any(record.exc_info and record.exc_info[1] is psutil_identity_error for record in caplog.records)
    assert str(psutil_identity_error) in caplog.text


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_linux_terminal_proc_states_have_no_live_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_PROC_ROOT", tmp_path)
    process_root = tmp_path / "4242"
    process_root.mkdir()
    stat_fields = [state, *(["0"] * 18), "12345"]
    (process_root / "stat").write_text(
        f"4242 (python) {' '.join(stat_fields)}",
        encoding="utf-8",
    )

    assert probe_process_identity(4242) is None


def test_linux_proc_identity_reports_vanishing_entry_as_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取 /proc 时条目消失按已核实终结收口，其余错误显式抛出。"""

    class VanishingStatFile:
        def read_text(
            self, encoding: str | None = None, errors: str | None = None
        ) -> str:
            raise FileNotFoundError(2, "No such file or directory")

    class VanishingProcessDirectory:
        def __truediv__(self, name: str) -> VanishingStatFile:
            return VanishingStatFile()

    class VanishingProcRoot:
        def __truediv__(self, name: str) -> VanishingProcessDirectory:
            return VanishingProcessDirectory()

    class DeniedStatFile:
        def read_text(
            self, encoding: str | None = None, errors: str | None = None
        ) -> str:
            raise PermissionError(13, "Permission denied")

    class DeniedProcessDirectory:
        def __truediv__(self, name: str) -> DeniedStatFile:
            return DeniedStatFile()

    class DeniedProcRoot:
        def __truediv__(self, name: str) -> DeniedProcessDirectory:
            return DeniedProcessDirectory()

    monkeypatch.setattr(process_identity, "_PROC_ROOT", VanishingProcRoot())
    # 进程在 read_text 期间消失：与"条目不存在"同一事实 ⇒ 已核实终结。
    assert process_identity._probe_linux_proc_identity(4242) is None

    monkeypatch.setattr(process_identity, "_PROC_ROOT", DeniedProcRoot())
    # 非消失类读取错误仍然显式抛出，绝不默默吞掉。
    with pytest.raises(PermissionError):
        process_identity._probe_linux_proc_identity(4242)
