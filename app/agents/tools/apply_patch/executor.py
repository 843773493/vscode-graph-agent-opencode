from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import tempfile

from app.agents.tools.apply_patch.journal import (
    delete_apply_patch_journal,
    write_apply_patch_journal,
)
from app.agents.tools.apply_patch.models import ActionType, Commit
from app.agents.tools.apply_patch.parser import (
    identify_files_added,
    identify_files_affected,
    identify_files_needed,
    parse_patch,
)
from app.core.path_utils import get_workspace_root


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    requested_path: str
    virtual_path: str
    real_path: Path
    existed: bool
    content: str
    mode: int | None


@dataclass(frozen=True, slots=True)
class AffectedFile:
    path: str
    operation: str


def apply_patch_text(
    patch_text: str,
    *,
    explanation: str,
    workspace_root: Path | None = None,
) -> dict[str, object]:
    root = (workspace_root or get_workspace_root()).resolve()
    needed_paths = identify_files_needed(patch_text)
    added_paths = identify_files_added(patch_text)
    _validate_declared_paths(needed_paths + added_paths, root)
    current_files = _load_current_files(needed_paths, root)
    _validate_added_files(added_paths, root)
    commit = parse_patch(patch_text, current_files)
    snapshots = _capture_snapshots(_commit_paths(commit), root)
    journal_id = write_apply_patch_journal(
        [_journal_payload(item) for item in snapshots],
        workspace_root=root,
    )

    try:
        affected_files = _apply_commit_transactionally(commit, snapshots, root)
    except BaseException:
        delete_apply_patch_journal(journal_id, workspace_root=root)
        raise

    return {
        "status": "success",
        "explanation": explanation,
        "journal_id": journal_id,
        "fuzz": int(commit.fuzz),
        "files": [
            {"path": item.path, "operation": item.operation}
            for item in affected_files
        ],
    }


def extract_apply_patch_file_paths(
    patch_text: str,
    *,
    workspace_root: Path | None = None,
) -> list[str]:
    paths = identify_files_affected(patch_text)
    _validate_declared_paths(paths, (workspace_root or get_workspace_root()).resolve())
    return paths


def _load_current_files(paths: list[str], workspace_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for requested_path in paths:
        real_path = _resolve_workspace_file(requested_path, workspace_root)
        if not real_path.is_file():
            raise FileNotFoundError(f"File not found: {requested_path}")
        result[requested_path] = real_path.read_text(encoding="utf-8")
    return result


def _validate_added_files(paths: list[str], workspace_root: Path) -> None:
    for requested_path in paths:
        real_path = _resolve_workspace_file(requested_path, workspace_root)
        if real_path.exists():
            raise FileExistsError(f"Add File target already exists: {requested_path}")


def _validate_declared_paths(paths: list[str], workspace_root: Path) -> None:
    if len(paths) != len(set(paths)):
        duplicate = next(path for path in paths if paths.count(path) > 1)
        raise ValueError(f"Duplicate patch path: {duplicate}")
    for path in paths:
        _resolve_workspace_file(path, workspace_root)


def _commit_paths(commit: Commit) -> list[str]:
    paths: dict[str, None] = {}
    for path, change in commit.changes.items():
        paths.setdefault(path, None)
        if change.move_path:
            paths.setdefault(change.move_path, None)
    return list(paths)


def _capture_snapshots(paths: list[str], workspace_root: Path) -> list[FileSnapshot]:
    snapshots: list[FileSnapshot] = []
    for requested_path in paths:
        real_path = _resolve_workspace_file(requested_path, workspace_root)
        if real_path.exists() and not real_path.is_file():
            raise RuntimeError(f"apply_patch 只能修改普通文件: {requested_path}")
        existed = real_path.exists()
        snapshots.append(
            FileSnapshot(
                requested_path=requested_path,
                virtual_path=_virtual_path(real_path, workspace_root),
                real_path=real_path,
                existed=existed,
                content=real_path.read_text(encoding="utf-8") if existed else "",
                mode=real_path.stat().st_mode if existed else None,
            )
        )
    return snapshots


def _journal_payload(snapshot: FileSnapshot) -> dict[str, object]:
    return {
        "file_path": snapshot.virtual_path,
        "before_exists": snapshot.existed,
        "before_content": snapshot.content,
    }


def _apply_commit_transactionally(
    commit: Commit,
    snapshots: list[FileSnapshot],
    workspace_root: Path,
) -> list[AffectedFile]:
    affected: list[AffectedFile] = []
    try:
        for requested_path, change in commit.changes.items():
            source = _resolve_workspace_file(requested_path, workspace_root)
            if change.type == ActionType.DELETE:
                source.unlink()
                affected.append(AffectedFile(requested_path, "delete"))
            elif change.type == ActionType.ADD:
                _atomic_write(source, change.new_content or "", mode=None)
                affected.append(AffectedFile(requested_path, "add"))
            elif change.move_path:
                target = _resolve_workspace_file(change.move_path, workspace_root)
                _atomic_write(target, change.new_content or "", mode=_existing_mode(source))
                source.unlink()
                affected.append(AffectedFile(requested_path, "move"))
                affected.append(AffectedFile(change.move_path, "move"))
            else:
                _atomic_write(source, change.new_content or "", mode=_existing_mode(source))
                affected.append(AffectedFile(requested_path, "update"))
    except BaseException as exc:
        try:
            _restore_snapshots(snapshots)
        except BaseException as rollback_error:
            raise RuntimeError(
                "apply_patch 执行失败且工作区回滚失败: "
                f"apply_error={exc!r}; rollback_error={rollback_error!r}"
            ) from rollback_error
        raise
    return affected


def _restore_snapshots(snapshots: list[FileSnapshot]) -> None:
    for snapshot in snapshots:
        if snapshot.existed:
            _atomic_write(snapshot.real_path, snapshot.content, mode=snapshot.mode)
        elif snapshot.real_path.exists():
            if not snapshot.real_path.is_file():
                raise RuntimeError(f"回滚目标不是普通文件: {snapshot.real_path}")
            snapshot.real_path.unlink()


def _atomic_write(path: Path, content: str, *, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise


def _existing_mode(path: Path) -> int | None:
    return path.stat().st_mode if path.exists() else None


def _resolve_workspace_file(file_path: str, workspace_root: Path) -> Path:
    raw_path = file_path.strip()
    if not raw_path:
        raise ValueError("文件路径不能为空")
    if raw_path.startswith(("/", "\\")) or PureWindowsPath(raw_path).drive:
        raise ValueError(
            f"apply_patch 文件路径必须是工作区相对路径，不能以 / 开头: {file_path}"
        )

    workspace_root = workspace_root.resolve()
    real_path = (workspace_root / raw_path).resolve()
    try:
        real_path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"文件路径超出工作区: {file_path}") from exc
    return real_path


def _virtual_path(real_path: Path, workspace_root: Path) -> str:
    workspace_root = workspace_root.resolve()
    return "/" + real_path.relative_to(workspace_root).as_posix()
