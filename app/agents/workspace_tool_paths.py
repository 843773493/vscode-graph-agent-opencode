from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from deepagents.backends.utils import validate_path

from app.core.exceptions import ForbiddenError
from app.core.path_utils import resolve_native_file_path, safe_join
from app.core.resource_uri import parse_resource_uri
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


def normalize_workspace_relative_path(
    raw_path: str,
    *,
    field_name: str = "path",
    workspace_root: Path | None = None,
) -> str:
    """把受限工作区工具的用户目标转为内部相对路径；绝不猜测虚拟根。"""
    normalized_input = raw_path
    if not normalized_input:
        raise ValueError(f"{field_name} 不能为空")
    if "\x00" in normalized_input:
        raise ValueError(f"{field_name} 不能包含 NUL 字符")
    if "://" in normalized_input:
        if workspace_root is None:
            raise ValueError(f"{field_name} 的资源 URI 必须绑定明确目标工作区")
        # 当前适配器仅公布工作区文件能力；Gateway 根交其实际 owner，不在此回退。
        parsed = parse_resource_uri(normalized_input)
        if parsed.scope != "workspace" or parsed.kind != "files":
            raise ValueError(f"{field_name} 超出当前工作区工具的访问能力")
        owner = WorkspaceFilePaths(
            workspace=workspace_root.resolve(), workspace_id=read_workspace_id(workspace_root), bindings=None,
        )
        try:
            target, _uri = owner.resolve(normalized_input)
        except ForbiddenError as error:
            raise ValueError(f"{field_name} 超出当前工作区工具的访问能力") from error
        normalized_input = target.relative_to(owner.workspace).as_posix()
    elif workspace_root is not None:
        if ".." in Path(normalized_input).parts:
            raise ValueError(f"{field_name} 不是有效的工作区相对路径: 不允许上级目录")
        try:
            target = resolve_native_file_path(workspace_root, normalized_input)
        except ForbiddenError as error:
            raise ValueError(f"{field_name} 超出当前工作区工具的访问能力") from error
        normalized_input = target.relative_to(workspace_root.resolve()).as_posix()
    elif Path(normalized_input).is_absolute():
        raise ValueError(f"{field_name} 的绝对路径必须绑定明确目标工作区")
    if os.name != "nt" and "\\" in normalized_input:
        raise ValueError(f"{field_name} 含目标平台的字面反斜杠，当前虚拟后端不支持该文件名")
    try:
        normalized = validate_path(normalized_input)
    except ValueError as error:
        raise ValueError(f"{field_name} 不是有效的工作区相对路径: {error}") from error
    if normalized == "/.":
        return "."
    return normalized.lstrip("/")



@dataclass(frozen=True, slots=True)
class WorkspaceToolPathResolver:
    """统一模型文件工具与源码调试工具的工作区路径契约。"""

    workspace_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", self.workspace_root.resolve())

    def normalize_relative_path(
        self,
        raw_path: str,
        *,
        field_name: str = "path",
    ) -> str:
        return normalize_workspace_relative_path(
            raw_path,
            field_name=field_name,
            workspace_root=self.workspace_root,
        )

    def backend_virtual_path(
        self,
        raw_path: str,
        *,
        field_name: str = "path",
    ) -> str:
        relative_path = self.normalize_relative_path(
            raw_path,
            field_name=field_name,
        )
        return "/" if relative_path == "." else f"/{relative_path}"

    def resolve_workspace_path(
        self,
        raw_path: str,
        *,
        field_name: str = "path",
    ) -> Path:
        relative_path = self.normalize_relative_path(
            raw_path,
            field_name=field_name,
        )
        try:
            return safe_join(
                self.workspace_root,
                "" if relative_path == "." else relative_path,
            )
        except ForbiddenError as error:
            raise ValueError(
                f"{field_name} 必须位于当前 workspace 内: {raw_path}"
            ) from error

    def workspace_relative_path(
        self,
        raw_path: str,
        *,
        field_name: str = "path",
    ) -> str:
        return self.normalize_relative_path(raw_path, field_name=field_name)


def backend_virtual_to_workspace_relative(path: str) -> str:
    """把 DeepAgents 返回的虚拟绝对路径转换为模型可见相对路径。"""
    normalized = validate_path(path)
    return "." if normalized in {"/", "/."} else normalized.lstrip("/")


__all__ = [
    "WorkspaceToolPathResolver",
    "backend_virtual_to_workspace_relative",
    "normalize_workspace_relative_path",
]
