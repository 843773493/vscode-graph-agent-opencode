from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from deepagents.backends.utils import validate_path

from app.core.exceptions import ForbiddenError
from app.core.path_utils import safe_join


def normalize_workspace_relative_path(
    raw_path: str,
    *,
    field_name: str = "path",
) -> str:
    """把模型输入规范化为标准工作区相对路径。"""
    normalized_input = raw_path.strip()
    if not normalized_input:
        raise ValueError(f"{field_name} 不能为空")
    if "\x00" in normalized_input:
        raise ValueError(f"{field_name} 不能包含 NUL 字符")
    normalized_input = normalized_input.replace("\\", "/")
    if normalized_input.startswith("/"):
        raise ValueError(
            f"{field_name} 必须是工作区相对路径，不能以 / 开头: {raw_path}"
        )
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
        return normalize_workspace_relative_path(raw_path, field_name=field_name)

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
