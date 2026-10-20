from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import ForbiddenError
from app.core.path_utils import resolve_native_file_path
from app.core.resource_uri import parse_resource_uri
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


@dataclass(frozen=True, slots=True)
class WorkspaceToolPathResolver:
    """将源码调试目标解析为工作区相对路径，不解释框架虚拟路径。"""

    workspace_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", self.workspace_root.resolve())

    def workspace_relative_path(self, raw_path: str, *, field_name: str = "path") -> str:
        if not raw_path:
            raise ValueError(f"{field_name} 不能为空")
        if "\x00" in raw_path:
            raise ValueError(f"{field_name} 不能包含 NUL 字符")
        try:
            if "://" in raw_path:
                parsed = parse_resource_uri(raw_path)
                if parsed.scope != "workspace" or parsed.kind != "files":
                    raise ValueError(f"{field_name} 超出当前工作区工具的访问能力")
                owner = WorkspaceFilePaths(
                    workspace=self.workspace_root,
                    workspace_id=read_workspace_id(self.workspace_root), bindings=None,
                )
                target, _uri = owner.resolve(raw_path)
            else:
                if ".." in Path(raw_path).parts:
                    raise ValueError(f"{field_name} 不是有效的工作区相对路径: 不允许上级目录")
                target = resolve_native_file_path(self.workspace_root, raw_path)
        except ForbiddenError as error:
            raise ValueError(f"{field_name} 超出当前工作区工具的访问能力") from error
        relative = target.relative_to(self.workspace_root).as_posix()
        if os.name != "nt" and "\\" in relative:
            raise ValueError(f"{field_name} 含目标平台的字面反斜杠，源码调试不支持该文件名")
        return relative
