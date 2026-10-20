"""V4A 解析与内容准备；实际发布交统一文本 owner。"""
from __future__ import annotations

from app.services.infrastructure.workspace_files.text_mutation import (
    FileSnapshot,
    TextPublication,
)
from app.services.infrastructure.workspace_files.text_patch.models import (
    ActionType,
    Commit,
)
from app.services.infrastructure.workspace_files.text_patch.parser import (
    identify_files_added,
    identify_files_affected,
    identify_files_needed,
    parse_patch,
)
from app.services.infrastructure.workspace_files.text_publication import (
    FilePreimage,
    WorkspaceFileConflictError,
)


class PatchPreparation:
    def __init__(self, patch_text: str) -> None:
        self._input = patch_text
        self.declared = identify_files_affected(patch_text)
        self.fuzz = 0

    def prepare(self, snapshots: dict[str, FileSnapshot]) -> list[TextPublication]:
        current = {name: snapshots[name].before.content.decode("utf-8").replace(_line_ending(snapshots[name].before), "\n")
                   for name in identify_files_needed(self._input) if snapshots[name].before is not None}
        for name in identify_files_added(self._input):
            if snapshots[name].before is not None:
                raise WorkspaceFileConflictError(f"新增目标不满足 expected_absent: {snapshots[name].uri}")
        commit = parse_patch(self._input, current)
        self.fuzz = int(commit.fuzz)
        return _prepare_steps(commit, snapshots)


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
