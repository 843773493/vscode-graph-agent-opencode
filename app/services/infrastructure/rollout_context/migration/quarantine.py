"""一次性 legacy import 的隔离记录；只保存坐标和受保护原件引用。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.migration.artifacts import (
    require_safe_path,
    sync_directory,
    write_private,
)


def write_quarantine(
    audit_root: Path,
    *,
    raw_artifact_ref: str,
    candidates: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """为被拒 candidate 建立独立、无正文的 quarantine audit。

    原始 JSONL 只存在于同一 audit 下的受保护 source snapshot；此文件只写
    candidate 坐标、拒绝状态和 raw_ref，避免 report/quarantine 泄露正文。
    """
    require_safe_path(audit_root)
    entries = [dict(candidate) for candidate in candidates]
    result = {
        "schema": "legacy-import-quarantine:v1",
        "raw_artifact_ref": raw_artifact_ref,
        "candidate_count": len(entries),
        "candidates": entries,
    }
    destination = audit_root / "quarantine.json"
    write_private(destination, canonical_json_bytes(result) + b"\n")
    sync_directory(audit_root)
    return {
        "path": str(destination),
        "schema": result["schema"],
        "candidate_count": len(entries),
        "raw_artifact_ref": raw_artifact_ref,
    }


__all__ = ["write_quarantine"]
