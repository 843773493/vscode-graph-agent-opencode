"""V4A 解析与内容准备；实际发布交统一文本 owner。"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.agents.tools.apply_patch.models import ActionType, Commit, DiffError
from app.agents.tools.apply_patch.parser import (
    identify_files_added,
    identify_files_affected,
    identify_files_needed,
    parse_patch,
)
from app.core.path_utils import get_workspace_root
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_files.text_mutation import (
    FileSnapshot,
    TextPublication,
    execute_text_mutation,
    validate_text_paths,
)
from app.services.infrastructure.workspace_files.text_publication import (
    FilePreimage,
    WorkspaceFileConflictError,
)


def apply_patch_text(patch_text: str, *, explanation: str, expected_versions: dict[str, str],
                     workspace_root: Path | None = None, target: str = ".",
                     authorize: Callable[[str], None] | None = None) -> dict[str, object]:
    workspace = (workspace_root or get_workspace_root()).resolve()
    paths = WorkspaceFilePaths(workspace=workspace, workspace_id=read_workspace_id(workspace), bindings=None)
    declared = identify_files_affected(patch_text)
    fuzz = 0

    def prepare(snapshots: dict[str, FileSnapshot]) -> list[TextPublication]:
        nonlocal fuzz
        current = {name: snapshots[name].before.content.decode("utf-8").replace(_line_ending(snapshots[name].before), "\n")
                   for name in identify_files_needed(patch_text) if snapshots[name].before is not None}
        for name in identify_files_added(patch_text):
            if snapshots[name].before is not None:
                raise WorkspaceFileConflictError(f"新增目标不满足 expected_absent: {snapshots[name].uri}")
        commit = parse_patch(patch_text, current)
        fuzz = int(commit.fuzz)
        return _prepare_steps(commit, snapshots)

    try:
        result = execute_text_mutation(paths=paths, target=target, declared=declared, expected_versions=expected_versions,
                                       prepare=prepare, explanation=explanation, authorize=authorize)
    except ValueError as error:
        # 补丁格式错误保留其公开异常类型；内容版本冲突独立于补丁语法。
        if type(error) is ValueError:
            raise DiffError(str(error)) from error
        raise
    result["fuzz"] = fuzz
    return result


def extract_apply_patch_file_paths(patch_text: str, *, workspace_root: Path | None = None) -> list[str]:
    paths = identify_files_affected(patch_text)
    validate_text_paths(paths, (workspace_root or get_workspace_root()).resolve())
    return paths


def _prepare_steps(commit: Commit, snapshots: dict[str, FileSnapshot]) -> list[TextPublication]:
    steps = []
    for name, change in commit.changes.items():
        source = snapshots[name]
        # 补丁匹配逻辑行；纯 CRLF 原件发布时保留其行结尾，版本始终按原字节。
        content = (change.new_content or "").replace("\n", _line_ending(source.before)).encode("utf-8")
        if change.type == ActionType.DELETE:
            steps.append(TextPublication(source, None, "delete"))
        elif change.move_path:
            destination = snapshots[change.move_path]
            steps.append(TextPublication(destination, content, "move", source.before.mode))
            steps.append(TextPublication(source, None, "move"))
        else:
            steps.append(TextPublication(source, content, change.type.value))
    return steps


def _line_ending(before: FilePreimage | None) -> str:
    return "\r\n" if before is not None and b"\r\n" in before.content and b"\n" not in before.content.replace(b"\r\n", b"") else "\n"
