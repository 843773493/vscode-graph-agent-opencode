"""捕获 marker 的原生 I/O 失败保留原错，不能发布残缺冻结状态。"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from app.core.session_catalog_store.contracts import (
    ForkSourceCaptureRecord,
    fork_source_capture_relative_locator,
)
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.services.infrastructure.rollout_context.fork import source_capture as capture
from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    ForkRolloutSourceSnapshot,
    ForkSourceSnapshot,
    _tree_manifest,
)


@dataclass(frozen=True)
class CaptureFiles:
    sessions: Path
    root: Path
    record: ForkSourceCaptureRecord
    snapshot: ForkSourceSnapshot

    def bytes(self) -> dict[str, bytes]:
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}


@pytest.fixture
def capture_files(tmp_path: Path) -> CaptureFiles:
    sessions = tmp_path / "sessions"
    identifier = "a" * 32
    record = ForkSourceCaptureRecord(
        copy_operation_id="copy-test", creation_key=None,
        source_session_id="ses_00000000400070008000000000000001", source_thread_id="thr_00000000400070008000000000000001",
        target_session_id="ses_00000000400070008000000000000002", target_thread_id="thr_00000000400070008000000000000002",
        snapshot_id=identifier, snapshot_relative_locator=fork_source_capture_relative_locator(identifier),
        fork_id="fork-test", materialization_id="materialization-test", request_json="{}", preimage_hash="preimage",
        state="preparing", snapshot_sha256=None, marker_sha256=None, abort_reason=None,
    )
    root = capture.capture_directory(sessions, record)
    node = root / record.source_session_id
    (node / "rollout").mkdir(parents=True)
    (node / "rollout/rollout.jsonl").write_bytes(b"unit-canonical\n")
    manifest = b"unit-manifest\n"
    (node / "snapshot-manifest.json").write_bytes(manifest)
    control = b"unit-control\n"
    (node / CONTROL_DATABASE_NAME).write_bytes(control)
    snapshot = ForkSourceSnapshot(ForkRolloutSourceSnapshot(
        source_session_id=record.source_session_id, source_thread_id=record.source_thread_id,
        captured_root=node, files=_tree_manifest(node / "rollout"), manifest_bytes=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        control_sha256=hashlib.sha256(CONTROL_DATABASE_NAME.encode() + control).hexdigest(), source_lifecycle_generation=1,
    ))
    return CaptureFiles(sessions, root, record, snapshot)


@pytest.fixture
def frozen(capture_files: CaptureFiles) -> CaptureFiles:
    item = capture_files
    marker = capture.publish_captured_snapshot(item.record, item.snapshot)
    return replace(item, record=replace(item.record, state="source_captured", marker_sha256=marker, snapshot_sha256=item.snapshot.snapshot_sha256))


@pytest.mark.parametrize("phase", ["scan", "file_fsync", "write", "replace", "final_fsync"])
def test_publication_native_failure_keeps_original_and_never_claims_a_new_hash(
    capture_files: CaptureFiles, phase: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    item = capture_files
    original = item.bytes()
    native = PermissionError(errno.EACCES, f"原生失败 {item.root}", str(item.root))
    owner, operation = {
        "scan": (os, "scandir"), "file_fsync": (capture, "fsync_file"), "write": (Path, "open"),
        "replace": (os, "replace"), "final_fsync": (capture, "fsync_directory"),
    }[phase]
    original_operation = getattr(owner, operation)

    def fail(path, *args, **kwargs):
        selected = (
            (phase == "scan" and not isinstance(path, int) and Path(path) == item.root)
            or (phase == "file_fsync" and Path(path).name == "snapshot-manifest.json")
            or (phase == "write" and Path(path).name == "captured.preparing")
            or (phase == "replace" and Path(path).name == "captured.preparing")
            or (phase == "final_fsync" and Path(path) == item.root and (item.root / "captured.json").exists())
        )
        if selected:
            raise native
        return original_operation(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner, operation, fail)
        with pytest.raises(PermissionError) as caught:
            capture.publish_captured_snapshot(item.record, item.snapshot)
    assert caught.value.__cause__ is native and caught.value.errno == errno.EACCES
    assert caught.value.filename is None and str(item.root) not in str(caught.value)
    assert "artifact=fork_capture stage=publication" in str(caught.value) and str(item.root) in caplog.text
    assert all((item.root / path).read_bytes() == raw for path, raw in original.items())
    assert item.record.state == "preparing" and item.record.snapshot_sha256 is None
    if phase == "final_fsync":
        restored, marker = capture.read_captured_snapshot(item.sessions, item.record)
        assert restored == item.snapshot and marker == hashlib.sha256((item.root / "captured.json").read_bytes()).hexdigest()
    else:
        assert not (item.root / "captured.json").exists()


@pytest.mark.parametrize("phase", ["location", "marker_read", "manifest_read", "control_read", "attachment_inspection", "cleanup"])
def test_frozen_capture_io_error_keeps_identity_bytes_cause_and_retry(
    frozen: CaptureFiles, phase: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    item = frozen
    original = item.bytes()
    fault = {
        "location": item.root.parent, "marker_read": item.root / "captured.json",
        "manifest_read": item.snapshot.captured_root / "snapshot-manifest.json",
        "control_read": item.snapshot.captured_root / CONTROL_DATABASE_NAME,
        "attachment_inspection": item.snapshot.captured_root / "attachment-files", "cleanup": item.root,
    }[phase]
    owner, operation = {
        "location": (Path, "is_symlink"), "marker_read": (Path, "read_bytes"), "manifest_read": (Path, "read_bytes"),
        "control_read": (Path, "open"), "attachment_inspection": (Path, "is_dir"), "cleanup": (shutil, "rmtree"),
    }[phase]
    original_operation = getattr(owner, operation)
    native = PermissionError(errno.EACCES, f"原生失败 {fault}", str(fault))

    def fail(path, *args, **kwargs):
        if Path(path) == fault:
            raise native
        return original_operation(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner, operation, fail)
        with pytest.raises(PermissionError) as caught:
            if phase == "cleanup":
                capture.remove_captured_snapshot(item.sessions, item.record)
            else:
                capture.read_captured_snapshot(item.sessions, item.record)
    assert caught.value.__cause__ is native and caught.value.errno == errno.EACCES
    assert caught.value.filename is None and str(item.root) not in str(caught.value)
    assert f"artifact=fork_capture stage={phase}" in str(caught.value) and str(fault) in caplog.text
    assert item.bytes() == original
    restored, marker = capture.read_captured_snapshot(item.sessions, item.record)
    assert restored == item.snapshot and marker == item.record.marker_sha256
    if phase == "cleanup":
        capture.remove_captured_snapshot(item.sessions, item.record)
        assert not item.root.exists()


def test_marker_publication_rejects_symbolic_directory_without_publishing(capture_files: CaptureFiles) -> None:
    item = capture_files
    link = item.root / "private-symlink"
    link.symlink_to(item.snapshot.captured_root, target_is_directory=True)
    with pytest.raises(RuntimeError, match="含符号链接目录"):
        capture.publish_captured_snapshot(item.record, item.snapshot)
    assert link.is_symlink() and not (item.root / "captured.json").exists()


@pytest.mark.parametrize("committed", [False, True])
def test_missing_marker_preserves_preparing_and_committed_distinction(frozen: CaptureFiles, committed: bool) -> None:
    item = frozen
    (item.root / "captured.json").unlink()
    record = item.record if committed else replace(item.record, state="preparing", snapshot_sha256=None, marker_sha256=None)
    with pytest.raises(RuntimeError) as caught:
        capture.read_captured_snapshot(item.sessions, record)
    assert isinstance(caught.value, capture.IncompleteForkSourceCaptureError) is not committed
    assert str(item.root) not in str(caught.value)
