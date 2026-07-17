from __future__ import annotations

import json
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_workspace_root


APPLY_PATCH_JOURNAL_DIR = "apply_patch"


def write_apply_patch_journal(
    snapshots: list[dict[str, object]],
    *,
    workspace_root: Path | None = None,
) -> str:
    journal_root = _journal_root(workspace_root)
    journal_root.mkdir(parents=True, exist_ok=True)
    journal_id = create_prefixed_id("patch")
    journal_path = journal_root / f"{journal_id}.json"
    journal_path.write_text(
        json.dumps(
            {"journal_id": journal_id, "snapshots": snapshots},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return journal_id


def delete_apply_patch_journal(
    journal_id: str,
    *,
    workspace_root: Path | None = None,
) -> None:
    journal_path = _journal_root(workspace_root) / f"{journal_id}.json"
    if journal_path.exists():
        journal_path.unlink()


def load_apply_patch_journal_from_result(
    result_text: str,
    *,
    workspace_root: Path | None = None,
) -> list[dict[str, object]]:
    try:
        result = json.loads(result_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("apply_patch 工具结果不是合法 JSON，无法读取变更 journal") from exc
    if not isinstance(result, dict):
        raise RuntimeError("apply_patch 工具结果格式错误，无法读取变更 journal")
    journal_id = result.get("journal_id")
    if not isinstance(journal_id, str) or not journal_id.strip():
        raise RuntimeError("apply_patch 工具结果缺少 journal_id，无法记录文件变更")
    journal_path = _journal_root(workspace_root) / f"{journal_id}.json"
    if not journal_path.is_file():
        raise RuntimeError(f"apply_patch journal 缺失: {journal_path}")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"apply_patch journal 损坏: {journal_path}") from exc
    snapshots = journal.get("snapshots") if isinstance(journal, dict) else None
    if not isinstance(snapshots, list):
        raise RuntimeError(f"apply_patch journal 格式错误: {journal_path}")
    return [snapshot for snapshot in snapshots if isinstance(snapshot, dict)]


def _journal_root(workspace_root: Path | None = None) -> Path:
    root = (workspace_root or get_workspace_root()).resolve()
    return root / ".boxteam" / "cache" / APPLY_PATCH_JOURNAL_DIR
