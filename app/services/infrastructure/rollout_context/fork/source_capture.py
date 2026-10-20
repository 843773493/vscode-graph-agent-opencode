"""创建 journal 指定 locator 的 captured marker：完整 typed facts 与定点校验。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from app.core.session_catalog_store.contracts import ForkSourceCaptureRecord
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.attachment_blob_catalog.records import (
    ForkAttachmentSnapshot,
)
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugSourceCopySnapshot,
)
from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    ForkRolloutSourceSnapshot,
    ForkSnapshotFile,
    ForkSourceSnapshot,
    _fsync_directory,
    _tree_manifest,
)


class IncompleteForkSourceCaptureError(RuntimeError):
    """冻结 capture 仍 preparing，普通私有目录尚未发布 captured marker。"""

    code = "fork_source_capture_incomplete"
    owner = "fork_source_capture"

    def __init__(self, record: ForkSourceCaptureRecord) -> None:
        self.capture = record
        super().__init__(
            "fork source capture 未完成: "
            f"operation_id={record.copy_operation_id}, snapshot_id={record.snapshot_id}, "
            f"source={record.source_session_id}/{record.source_thread_id}, "
            f"target={record.target_session_id}/{record.target_thread_id}, "
            "state=preparing，普通私有目录缺少 captured marker"
        )

    def to_detail(self, *, action: str) -> dict[str, object]:
        """跨 HTTP 保留原 owner 的完整 operation/source/target 冻结身份。"""
        return {"owner": self.owner, "code": self.code, "action": action,
                "capture": asdict(self.capture), "message": str(self)}


def capture_directory(sessions_root: Path, record: ForkSourceCaptureRecord) -> Path:
    """locator 必须逐字匹配 journal 分配的 snapshot identity，正常 resolver 不可见。"""
    if record.snapshot_relative_locator != f"fork-source-captures/{record.snapshot_id}" or (
        len(record.snapshot_id) != 32 or any(char not in "0123456789abcdef" for char in record.snapshot_id)
    ):
        raise RuntimeError("fork capture 内部 locator 与预分配 identity 不一致")
    root = sessions_root.parent / "navigation" / "fork-source-captures"
    if root.is_symlink():
        raise RuntimeError("fork capture 内部目录是符号链接")
    return root / record.snapshot_id


def publish_captured_snapshot(record: ForkSourceCaptureRecord, snapshot: ForkSourceSnapshot) -> str:
    """source guard 内写 marker；所有快照 bytes fsync 后原子 rename 并 fsync 目录。"""
    payload = {
        "schema": "source-copy-snapshot:v2", "state": "captured",
        "copy_operation_id": record.copy_operation_id, "preimage_hash": record.preimage_hash,
        "source_snapshot_id": record.snapshot_id, "snapshot_sha256": snapshot.snapshot_sha256,
        "rollout": {
            "source_session_id": snapshot.source_session_id,
            "source_thread_id": snapshot.source_thread_id,
            "files": [asdict(item) for item in snapshot.rollout.files],
            "manifest_sha256": snapshot.rollout.manifest_sha256,
            "control_sha256": snapshot.rollout.control_sha256,
            "source_lifecycle_generation": snapshot.rollout.source_lifecycle_generation,
        },
        "attachments": [asdict(item) for item in snapshot.attachments],
        "node_debug": snapshot.node_debug.to_payload() if snapshot.node_debug is not None else None,
        "claim_ids": sorted(item.claim_id for item in snapshot.attachments if item.claim_id is not None),
    }
    payload["claim_manifest_sha256"] = hashlib.sha256(canonical_json_bytes(payload["claim_ids"])).hexdigest()
    encoded = canonical_json_bytes(payload)
    root = snapshot.captured_root.parent
    # 此处只遍历当前 operation 已登记的私有快照，不定位或吸收 source 业务目录。
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("fork captured snapshot 含非普通文件")
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        _fsync_directory(Path(directory))
    temporary = root / "captured.preparing"
    with temporary.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, root / "captured.json")
    _fsync_directory(root)
    _fsync_directory(root.parent)
    return hashlib.sha256(encoded).hexdigest()


def read_captured_snapshot(sessions_root: Path, record: ForkSourceCaptureRecord) -> tuple[ForkSourceSnapshot, str]:
    """恢复只接受完整 captured marker 和全部原 frozen hash，不读取 live source。"""
    root = capture_directory(sessions_root, record)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("fork source capture 缺少普通 snapshot 目录")
    marker = root / "captured.json"
    if marker.is_symlink():
        raise RuntimeError("fork source capture captured marker 是符号链接")
    if not marker.exists():
        if record.state == "preparing" and record.snapshot_sha256 is None and record.marker_sha256 is None:
            raise IncompleteForkSourceCaptureError(record)
        raise RuntimeError("fork source capture 已冻结 journal 缺少 captured marker")
    if not marker.is_file():
        raise RuntimeError("fork source capture 缺少普通 captured marker")
    encoded = marker.read_bytes()
    marker_hash = hashlib.sha256(encoded).hexdigest()
    raw = json.loads(encoded)
    if canonical_json_bytes(raw) != encoded or (
        raw["schema"], raw["state"], raw["copy_operation_id"], raw["preimage_hash"], raw["source_snapshot_id"],
    ) != ("source-copy-snapshot:v2", "captured", record.copy_operation_id, record.preimage_hash, record.snapshot_id):
        raise RuntimeError("fork captured marker identity/preimage 冲突")
    if record.marker_sha256 is not None and record.marker_sha256 != marker_hash:
        raise RuntimeError("fork captured marker hash 与 journal 不一致")
    rollout = raw["rollout"]
    captured_root = root / record.source_session_id
    manifest_bytes = (captured_root / "snapshot-manifest.json").read_bytes()
    if (rollout["source_session_id"], rollout["source_thread_id"]) != (record.source_session_id, record.source_thread_id) or (
        hashlib.sha256(manifest_bytes).hexdigest() != rollout["manifest_sha256"]
    ):
        raise RuntimeError("fork captured rollout manifest/owner 不一致")
    files = tuple(ForkSnapshotFile(**item) for item in rollout["files"])
    current_files = _tree_manifest(captured_root / "rollout")
    if current_files != files:
        frozen = {item.relative_path: (item.size_bytes, item.sha256) for item in files}
        current = {item.relative_path: (item.size_bytes, item.sha256) for item in current_files}
        changed = {name: {"frozen": frozen.get(name), "current": current.get(name)}
                   for name in sorted(frozen.keys() | current.keys()) if frozen.get(name) != current.get(name)}
        raise RuntimeError(f"fork captured rollout files hash/length 不一致: {changed}")
    digest = hashlib.sha256()
    for name in (CONTROL_DATABASE_NAME, CONTROL_DATABASE_NAME + "-wal", CONTROL_DATABASE_NAME + "-journal"):
        path = captured_root / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("fork captured session-control 含非普通文件")
            digest.update(name.encode())
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
    if digest.hexdigest() != rollout["control_sha256"]:
        raise RuntimeError("fork captured session-control hash 不一致")
    snapshot = ForkSourceSnapshot(
        rollout=ForkRolloutSourceSnapshot(
            source_session_id=record.source_session_id, source_thread_id=record.source_thread_id,
            captured_root=captured_root, files=files, manifest_bytes=manifest_bytes,
            manifest_sha256=rollout["manifest_sha256"], control_sha256=rollout["control_sha256"],
            source_lifecycle_generation=rollout["source_lifecycle_generation"],
        ),
        attachments=tuple(ForkAttachmentSnapshot(**item) for item in raw["attachments"]),
        node_debug=NodeDebugSourceCopySnapshot.from_payload(raw["node_debug"]) if raw["node_debug"] is not None else None,
    )
    claim_ids = sorted(item.claim_id for item in snapshot.attachments if item.claim_id is not None)
    if claim_ids != raw["claim_ids"] or hashlib.sha256(canonical_json_bytes(claim_ids)).hexdigest() != raw["claim_manifest_sha256"] or (
        snapshot.snapshot_sha256 != raw["snapshot_sha256"] or record.snapshot_sha256 not in {None, snapshot.snapshot_sha256}
    ):
        raise RuntimeError("fork captured snapshot/claim manifest hash 不一致")
    return snapshot, marker_hash


def remove_captured_snapshot(sessions_root: Path, record: ForkSourceCaptureRecord) -> None:
    root = capture_directory(sessions_root, record)
    if root.is_symlink():
        raise RuntimeError("fork capture 清理拒绝符号链接")
    if root.exists():
        shutil.rmtree(root)
        _fsync_directory(root.parent)
