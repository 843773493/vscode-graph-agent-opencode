from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.infrastructure.node_debug.process import process_identity
from app.services.infrastructure.node_debug.process.process_identity import (
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
