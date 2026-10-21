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
