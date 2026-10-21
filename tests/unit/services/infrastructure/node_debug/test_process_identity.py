from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.infrastructure.node_debug.process import process_identity
from app.services.infrastructure.node_debug.process.process_identity import (
    IDENTITY_SOURCE_PSUTIL,
    probe_process_identity,
)


def test_psutil_provides_verifiable_identity_when_proc_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
