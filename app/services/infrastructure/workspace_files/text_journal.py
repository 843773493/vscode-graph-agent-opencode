from __future__ import annotations

import json
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver, get_workspace_root
from app.core.session_thread_context import current_session_thread_owner

TEXT_MUTATION_JOURNAL_DIR = "text_mutations"


def write_text_mutation_journal(
    snapshots: list[dict[str, object]],
    *,
    workspace_root: Path | None = None,
) -> str:
    journal_root = _journal_root(workspace_root)
    journal_root.mkdir(parents=True, exist_ok=True)
    journal_id = create_prefixed_id("text")
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


def update_text_mutation_journal(
    journal_id: str,
    publications: list[dict[str, object]],
    *,
    workspace_root: Path | None = None,
) -> None:
    """把文本修改的逐文件发布结果追加到 journal。"""
    journal_path = _journal_root(workspace_root) / f"{journal_id}.json"
    if not journal_path.is_file():
        raise RuntimeError(f"text mutation journal 缺失: {journal_path}")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"text mutation journal 损坏: {journal_path}") from exc
    if not isinstance(journal, dict):
        raise TypeError(f"text mutation journal 格式错误: {journal_path}")
    journal["files"] = publications
    journal_path.write_text(
        json.dumps(journal, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_text_mutation_snapshots(
    result_text: str,
    *,
    workspace_root: Path | None = None,
) -> list[dict[str, object]]:
    try:
        result = json.loads(result_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("text mutation 工具结果不是合法 JSON，无法读取变更 journal") from exc
    if not isinstance(result, dict):
        raise TypeError("text mutation 工具结果格式错误，无法读取变更 journal")
    journal_id = result.get("journal_id")
    if not isinstance(journal_id, str) or not journal_id.strip():
        raise RuntimeError("text mutation 工具结果缺少 journal_id，无法记录文件变更")
    journal_path = _journal_root(workspace_root) / f"{journal_id}.json"
    if not journal_path.is_file():
        raise RuntimeError(f"text mutation journal 缺失: {journal_path}")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"text mutation journal 损坏: {journal_path}") from exc
    snapshots = journal.get("snapshots") if isinstance(journal, dict) else None
    if not isinstance(snapshots, list):
        raise TypeError(f"text mutation journal 格式错误: {journal_path}")
    return [snapshot for snapshot in snapshots if isinstance(snapshot, dict)]


def _journal_root(workspace_root: Path | None = None) -> Path:
    root = (workspace_root or get_workspace_root()).resolve()
    owner = current_session_thread_owner()
    if owner is not None:
        session = get_session_path_resolver(root / ".boxteam" / "sessions").resolve_session_node(owner.session_id)
        return session / "changes" / TEXT_MUTATION_JOURNAL_DIR
    # 独立工具调用没有 Session；临时 journal 仍只属于其显式隔离工作区。
    return root / ".boxteam" / "cache" / TEXT_MUTATION_JOURNAL_DIR
