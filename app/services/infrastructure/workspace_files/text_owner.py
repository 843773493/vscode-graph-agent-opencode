"""模型和目标工作区共用的文本修改入口；工具层只投影参数与回执。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_files.text_mutation import (
    FileSnapshot,
    TextPublication,
    execute_text_mutation,
    resolve_text_target,
)
from app.services.infrastructure.workspace_files.text_patch.executor import (
    PatchPreparation,
)
from app.services.infrastructure.workspace_files.text_patch.models import DiffError
from app.services.infrastructure.workspace_files.text_publication import (
    FilePreimage,
    WorkspaceFileConflictError,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateText:
    target: str
    content: str
    expected_version: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EditText:
    target: str
    old_string: str
    new_string: str
    expected_version: str
    replace_all: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class PatchText:
    target: str
    input: str
    expected_versions: dict[str, str]
    explanation: str


TextMutation = CreateText | EditText | PatchText


class WorkspaceTextMutationOwner:
    def __init__(self, *, paths: WorkspaceFilePaths) -> None:
        self._paths = paths

    def execute(self, request: TextMutation, *, authorize: Callable[[str], None] | None = None) -> dict[str, object]:
        if isinstance(request, PatchText):
            preparation = PatchPreparation(request.input)
            try:
                result = execute_text_mutation(
                    paths=self._paths, target=request.target, declared=preparation.declared,
                    expected_versions=request.expected_versions, prepare=preparation.prepare,
                    explanation=request.explanation, authorize=authorize,
                )
            except ValueError as error:
                # V4A 格式错误保留公开类型；内容版本冲突不归入补丁语法。
                if type(error) is ValueError:
                    raise DiffError(str(error)) from error
                raise
            result["fuzz"] = preparation.fuzz
            return result
        actual, uri = resolve_text_target(self._paths, request.target)
        relative = actual.relative_to(self._paths.workspace).as_posix()

        def prepare(snapshots: dict[str, FileSnapshot]) -> list[TextPublication]:
            snapshot = snapshots[relative]
            content = _create(request, snapshot.before) if isinstance(request, CreateText) else _edit(request, snapshot.before)
            return [TextPublication(snapshot, content, "add" if isinstance(request, CreateText) else "update")]

        result = execute_text_mutation(
            paths=self._paths, target=".", declared=[relative],
            expected_versions={relative: request.expected_version}, prepare=prepare, authorize=authorize,
        )
        result["target"] = uri
        return result

    async def execute_async(self, request: TextMutation, *, authorize: Callable[[str], None] | None = None) -> dict[str, object]:
        # 生命周期留在调用者事件循环；文件 I/O 和锁只在工作线程执行。
        return await asyncio.to_thread(self.execute, request, authorize=authorize)


def _create(request: CreateText, before: FilePreimage | None) -> bytes:
    if request.expected_version != "absent" or before is not None:
        raise WorkspaceFileConflictError("创建文件必须明确要求 expected_version=absent")
    return request.content.encode("utf-8")


def _edit(request: EditText, before: FilePreimage | None) -> bytes:
    if before is None:
        raise FileNotFoundError("精确编辑的目标文件不存在")
    if not request.old_string or request.old_string == request.new_string:
        raise ValueError("old_string 必须非空且与 new_string 不同")
    original = before.content.decode("utf-8")
    occurrences = original.count(request.old_string)
    if occurrences == 0 or (not request.replace_all and occurrences != 1):
        raise ValueError(f"精确编辑匹配数量无效: occurrences={occurrences}; 需要唯一匹配或明确 replace_all")
    return original.replace(request.old_string, request.new_string, -1 if request.replace_all else 1).encode("utf-8")
