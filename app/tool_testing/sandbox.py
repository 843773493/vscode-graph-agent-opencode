"""工具测试只向本次隔离目录授予文件能力，复用正式文件执行链。"""

from __future__ import annotations

import os
from pathlib import Path

from app.core.exceptions import ForbiddenError
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


class ToolTestFilePaths(WorkspaceFilePaths):
    def __init__(self, *, workspace_root: Path, attempt_root: Path) -> None:
        store = workspace_root.resolve() / ".boxteam" / "tool_tests"
        attempt = attempt_root.absolute()
        resolved = attempt.resolve()
        if (
            not attempt.is_relative_to(store)
            or resolved != attempt
            or resolved == store
            or any(os.path.normcase(part) == ".boxteam" for part in resolved.relative_to(store).parts)
        ):
            raise ForbiddenError("工具测试目录必须位于当前工作区 .boxteam/tool_tests 内")
        resolved.mkdir(parents=True)
        super().__init__(
            workspace=resolved,
            workspace_id=load_or_create_workspace_id(resolved),
            bindings=None,
        )

    def is_private(self, target: Path) -> bool:
        resolved = Path(os.path.realpath(target))
        if not target.is_relative_to(self.workspace) or not resolved.is_relative_to(self.workspace):
            return True
        return any(
            os.path.normcase(part) == ".boxteam"
            for part in (*target.relative_to(self.workspace).parts, *resolved.relative_to(self.workspace).parts)
        )
