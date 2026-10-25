"""JSONL 尾部恢复保留已提交前缀与原生故障证据。"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from app.services.infrastructure.rollout_context.storage import (
    rollout_maintenance_owner,
)
from app.services.infrastructure.rollout_context.storage.rollout_maintenance_owner import (
    reconcile_jsonl_tail,
)


@pytest.mark.parametrize("shape", ["missing", "directory"])
def test_missing_committed_file_reports_reason_without_location(
    tmp_path: Path, shape: str, caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "rollout.jsonl"
    if shape == "directory":
        path.mkdir()
    with pytest.raises(RuntimeError, match="committed JSONL 文件不存在") as caught:
        reconcile_jsonl_tail(path, 10)
    assert "artifact=rollout_jsonl stage=tail_reconciliation" in str(caught.value)
    assert str(path) not in str(caught.value) and str(path) in caplog.text
    assert path.is_dir() if shape == "directory" else not path.exists()


def test_offset_reconciliation_keeps_exact_committed_prefix(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    prefix = "已提交 Ω\n".encode()
    path.write_bytes(prefix)
    with pytest.raises(RuntimeError, match=f"file={len(prefix)}, committed={len(prefix) + 1}"):
        reconcile_jsonl_tail(path, len(prefix) + 1)
    assert path.read_bytes() == prefix
    original_mtime = path.stat().st_mtime_ns
    reconcile_jsonl_tail(path, len(prefix))
    assert path.stat().st_mtime_ns == original_mtime
    path.write_bytes(prefix + b"uncommitted tail")
    reconcile_jsonl_tail(path, len(prefix))
    assert path.read_bytes() == prefix


@pytest.mark.parametrize("stage", ["inspect", "open", "fsync"])
def test_native_tail_failure_keeps_cause_and_committed_bytes(
    tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "rollout.jsonl"
    prefix, tail = b"committed\n", b"uncommitted"
    path.write_bytes(prefix + tail)
    failure = PermissionError(errno.EACCES, f"原生故障 {path}", str(path))
    original_file, original_open = Path.is_file, Path.open

    def inspect(candidate):
        if candidate == path:
            raise failure
        return original_file(candidate)

    def open_file(candidate, *args, **kwargs):
        if candidate == path:
            raise failure
        return original_open(candidate, *args, **kwargs)

    def fsync(_descriptor):
        raise failure

    with monkeypatch.context() as patch:
        if stage == "inspect":
            patch.setattr(Path, "is_file", inspect)
        elif stage == "open":
            patch.setattr(Path, "open", open_file)
        else:
            patch.setattr(rollout_maintenance_owner.os, "fsync", fsync)
        with pytest.raises(PermissionError) as caught:
            reconcile_jsonl_tail(path, len(prefix))
    assert caught.value.__cause__ is failure
    assert caught.value.errno == errno.EACCES and caught.value.filename is None
    assert "artifact=rollout_jsonl stage=tail_reconciliation" in str(caught.value)
    assert str(path) not in str(caught.value) and str(path) in caplog.text
    assert path.read_bytes() == (prefix if stage == "fsync" else prefix + tail)
    reconcile_jsonl_tail(path, len(prefix))
    assert path.read_bytes() == prefix
