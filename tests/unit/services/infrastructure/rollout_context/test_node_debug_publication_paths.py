"""调试方案文件发布的局部错误合同，不代替执行或 fork 产品验收。"""

from __future__ import annotations

import errno
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.internal_v2.node_debug import NodeDebugSessionManifestDTO
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugSourceCopySnapshot,
)
from app.services.infrastructure.rollout_context.fork import (
    node_debug_materialization as publication,
)


@pytest.fixture
def snapshot() -> NodeDebugSourceCopySnapshot:
    raw = NodeDebugSessionManifestDTO(
        session_id="source", thread_id="thr_00000000400070008000000000000001", updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    ).model_dump_json().encode()
    digest = hashlib.sha256(raw).hexdigest()
    return NodeDebugSourceCopySnapshot(
        source_snapshot_id="snapshot", source_session_id="source", source_thread_id="thr_00000000400070008000000000000001",
        manifest_bytes=raw, manifest_size_bytes=len(raw), manifest_sha256=digest, manifest_revision=digest,
    )


def _stage(snapshot: NodeDebugSourceCopySnapshot, staging: Path) -> str:
    digest = publication._target_manifest_sha256(
        snapshot, target_session_id="target", target_thread_id="thr_00000000400070008000000000000002", configuration_id_map=(),
    )
    publication._stage_target_snapshot(
        snapshot, fork_id="fork", target_session_id="target", target_thread_id="thr_00000000400070008000000000000002",
        staging_root=staging, expected_manifest_sha256=digest,
    )
    return digest


@pytest.fixture
def staged(tmp_path: Path, snapshot: NodeDebugSourceCopySnapshot) -> tuple[Path, Path, str]:
    target = tmp_path / "private-target"
    staging = publication.target_staging_root(target / "rollout", "a" * 32)
    return target, staging, _stage(snapshot, staging)


@pytest.mark.parametrize("damage", ["missing", "parent_file", "existing_body"])
def test_publication_boundary_rejects_without_rewriting_paths_or_bytes(
    staged: tuple[Path, Path, str], damage: str, caplog: pytest.LogCaptureFixture,
) -> None:
    target, staging, _digest = staged
    before = (staging / "manifest.json").read_bytes()
    if damage == "parent_file":
        (target / "debug").write_bytes(b"original")
    elif damage == "existing_body":
        (target / "debug").mkdir()
        (target / "debug/original").write_bytes(b"original")
    path = staging / "missing" if damage == "missing" else staging
    with pytest.raises(RuntimeError) as caught:
        publication.publish_target_snapshot(path, target)
    assert "artifact=fork_debug stage=publication" in str(caught.value)
    assert str(target) not in str(caught.value) and str(target) in caplog.text
    assert (staging / "manifest.json").read_bytes() == before
    if damage == "parent_file":
        assert (target / "debug").read_bytes() == b"original"
    elif damage == "existing_body":
        assert (target / "debug/original").read_bytes() == b"original"
    else:
        assert not (target / "debug").exists()


@pytest.mark.parametrize("stage", ["staging_inspection", "staging", "publication", "verification"])
def test_native_error_keeps_cause_errno_and_recoverable_original(
    staged: tuple[Path, Path, str], snapshot: NodeDebugSourceCopySnapshot,
    stage: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    target, staging, digest = staged
    before = (staging / "manifest.json").read_bytes()
    if stage == "verification":
        publication.publish_target_snapshot(staging, target)
    operation = {
        "staging_inspection": "exists", "staging": "mkdir",
        "publication": "rename", "verification": "read_bytes",
    }[stage]
    original = getattr(Path, operation)
    new_staging = publication.target_staging_root(target / "rollout", "b" * 32)
    fault_path = (target / "debug/node/manifest.json" if stage == "verification"
                  else new_staging if stage.startswith("staging") else staging)
    native = PermissionError(errno.EACCES, f"原生故障 {fault_path}", str(fault_path))

    def reject(path: Path, *args, **kwargs):
        if path == fault_path:
            raise native
        return original(path, *args, **kwargs)

    def verify() -> None:
        publication.verify_published_target_snapshot(
            target, target_session_id="target", target_thread_id="thr_00000000400070008000000000000002",
            manifest_sha256=digest, source_configurations_json="[]", configuration_id_map_json="[]",
        )

    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, reject)
        with pytest.raises(PermissionError) as caught:
            if stage.startswith("staging"):
                _stage(snapshot, new_staging)
            elif stage == "publication":
                publication.publish_target_snapshot(staging, target)
            else:
                verify()
    assert caught.value.__cause__ is native and caught.value.errno == errno.EACCES
    assert caught.value.filename is None
    assert f"artifact=fork_debug stage={stage}" in str(caught.value)
    assert str(target) not in str(caught.value) and str(fault_path) in caplog.text
    preserved = target / "debug/node/manifest.json" if stage == "verification" else staging / "manifest.json"
    assert preserved.read_bytes() == before
    if stage != "verification":
        publication.publish_target_snapshot(staging, target)
    verify()
    assert (target / "debug/node/manifest.json").read_bytes() == before


def test_existing_staging_keeps_original(
    snapshot: NodeDebugSourceCopySnapshot, staged: tuple[Path, Path, str], caplog: pytest.LogCaptureFixture,
) -> None:
    target, staging, _digest = staged
    before = (staging / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError) as caught:
        _stage(snapshot, staging)
    assert "staging 已存在" in str(caught.value)
    assert str(target) not in str(caught.value) and str(staging) in caplog.text
    assert (staging / "manifest.json").read_bytes() == before
