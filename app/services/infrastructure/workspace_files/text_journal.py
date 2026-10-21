from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISREG

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver, get_workspace_root
from app.core.session_thread_context import current_session_thread_owner

TEXT_MUTATION_JOURNAL_DIR = "text_mutations"


def write_text_mutation_journal(
    snapshots: list[dict[str, object]],
    *,
    workspace_root: Path | None = None,
) -> str:
    journal_id = create_prefixed_id("text")
    with _journal_access("create"):
        journal_root = _journal_root(workspace_root)
        journal_root.mkdir(parents=True, exist_ok=True)
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
    observations: dict[str, dict[str, object]],
    workspace_root: Path | None = None,
) -> None:
    """把文本修改的逐文件发布结果追加到 journal。"""
    with _journal_access("update"):
        journal_path = _journal_root(workspace_root) / f"{journal_id}.json"
        journal = _read_journal(journal_path)
        journal["files"] = publications
        for snapshot in journal["snapshots"]:
            observation = observations.get(snapshot["uri"])
            if observation is not None:
                snapshot.update(observation)
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
    with _journal_access("read"):
        journal = _read_journal(_journal_root(workspace_root) / f"{journal_id}.json")
    snapshots = journal.get("snapshots")
    if not isinstance(snapshots, list):
        raise TypeError("text mutation journal 格式错误: artifact=text_mutation_journal")
    return [snapshot for snapshot in snapshots if isinstance(snapshot, dict)]


def _read_journal(path: Path) -> dict[str, object]:
    """读取与更新共用同一份 journal 形状和缺失检查。"""
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError) as error:
        raise RuntimeError("text mutation journal 缺失: artifact=text_mutation_journal") from error
    if not S_ISREG(mode):
        raise RuntimeError("text mutation journal 缺失: artifact=text_mutation_journal")
    journal = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(journal, dict):
        raise TypeError("text mutation journal 格式错误: artifact=text_mutation_journal")
    return journal


@contextmanager
def _journal_access(stage: str) -> Iterator[None]:
    """由存储 owner 发布逻辑阶段，原生位置和损坏内容保留在原因链。"""
    identity = f"artifact=text_mutation_journal, stage={stage}"
    try:
        yield
    except OSError as error:
        raise type(error)(error.errno, (
            f"{identity}, error={type(error).__name__}, errno={error.errno}"
        )) from error
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"text mutation journal 损坏: {identity}, "
            f"line={error.lineno}, column={error.colno}, position={error.pos}"
        ) from error
    except UnicodeDecodeError as error:
        raise UnicodeDecodeError(
            error.encoding, error.object, error.start, error.end,
            f"{identity}, error=UnicodeDecodeError",
        ) from error


def _journal_root(workspace_root: Path | None = None) -> Path:
    root = (workspace_root or get_workspace_root()).resolve()
    owner = current_session_thread_owner()
    if owner is not None:
        session = get_session_path_resolver(root / ".boxteam" / "sessions").resolve_session_node(owner.session_id)
        return session / "changes" / TEXT_MUTATION_JOURNAL_DIR
    # 独立工具调用没有 Session；临时 journal 仍只属于其显式隔离工作区。
    return root / ".boxteam" / "cache" / TEXT_MUTATION_JOURNAL_DIR
