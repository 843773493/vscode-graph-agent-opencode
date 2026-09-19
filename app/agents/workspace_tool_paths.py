from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from deepagents.backends.utils import validate_path

from app.core.exceptions import ForbiddenError
from app.core.path_utils import safe_join

_WINDOWS_DRIVE_PATTERN = re.compile(r"^[a-zA-Z]:")


def _looks_absolute(path: str) -> bool:
    """判断输入是否以绝对路径形式出现（POSIX 前导 / 或 Windows 盘符）。"""
    return path.startswith("/") or bool(_WINDOWS_DRIVE_PATTERN.match(path))


def _absolute_to_workspace_relative(
    path: str,
    *,
    workspace_root: Path | None,
) -> str:
    """把绝对路径收敛为工作区相对路径，绝不逃出工作区。

    提示词优先建议相对路径，但模型仍可能传入绝对路径。这里不再直接拒绝：
    位于工作区内的宿主机绝对路径被还原为其相对路径；其余以 `/` 开头的路径
    按 DeepAgents 虚拟绝对路径处理，去掉前导 `/` 后锚定到工作区根目录。
    Windows 盘符绝对路径保持原样，交由 validate_path 拒绝，避免盘符歧义。
    """
    if _WINDOWS_DRIVE_PATTERN.match(path):
        return path
    if workspace_root is not None:
        root = workspace_root.resolve()
        try:
            relative = Path(path).resolve().relative_to(root)
        except ValueError:
            pass
        else:
            return relative.as_posix() or "."
    stripped = path.lstrip("/")
    return stripped or "."


def normalize_workspace_relative_path(
    raw_path: str,
    *,
    field_name: str = "path",
    workspace_root: Path | None = None,
) -> str:
    """把模型输入规范化为标准工作区相对路径。

    规范优先接受工作区相对路径，同时兼容模型误传的绝对路径。传入
    workspace_root 时，工作区内的宿主机绝对路径会被精确还原为相对路径；
    未传时按 DeepAgents 虚拟绝对路径去掉前导 `/` 处理，仍锚定在工作区根。
    """
    normalized_input = raw_path.strip()
    if not normalized_input:
        raise ValueError(f"{field_name} 不能为空")
    if "\x00" in normalized_input:
        raise ValueError(f"{field_name} 不能包含 NUL 字符")
    normalized_input = _normalize_virtual_uri(
        normalized_input,
        field_name=field_name,
    )
    normalized_input = normalized_input.replace("\\", "/")
    if _looks_absolute(normalized_input):
        normalized_input = _absolute_to_workspace_relative(
            normalized_input,
            workspace_root=workspace_root,
        )
    try:
        normalized = validate_path(normalized_input)
    except ValueError as error:
        raise ValueError(f"{field_name} 不是有效的工作区相对路径: {error}") from error
    if normalized == "/.":
        return "."
    return normalized.lstrip("/")


def _normalize_virtual_uri(raw_path: str, *, field_name: str) -> str:
    """把受支持的虚拟 URI 转换成内部虚拟路径。"""

    if not raw_path.startswith("boxteam-session://"):
        return raw_path
    parsed = urlsplit(raw_path)
    if (
        parsed.scheme != "boxteam-session"
        or not parsed.netloc
        or not parsed.path.strip("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} 不是有效的 BoxTeam Session URI")
    return f"session-artifacts/{parsed.netloc}/{parsed.path.lstrip('/')}"


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
