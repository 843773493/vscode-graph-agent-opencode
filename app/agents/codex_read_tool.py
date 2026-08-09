import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemState
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_MAX_LINES = 2_000
_VIRTUAL_READ_PREFIXES = ("/.boxteam/", "/session-artifacts/")


class CodexReadFileSchema(BaseModel):
    """与 Codex `read` 工具一致的分页参数。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        description=(
            "File path to read. Use a workspace-relative path or an absolute host "
            "path."
        )
    )
    line_offset: int = Field(
        default=1,
        ge=1,
        description="1-indexed line number to start reading from.",
    )
    max_lines: int | None = Field(
        default=None,
        ge=1,
        description="Maximum number of lines to return. Omit to use the bounded default.",
    )


def _is_virtual_read_path(path: str) -> bool:
    return path in {"/.boxteam", "/session-artifacts"} or path.startswith(
        _VIRTUAL_READ_PREFIXES
    )


def _host_read_path(path: str, workspace_root: Path) -> str:
    requested = Path(path)
    resolved = requested.resolve() if requested.is_absolute() else (workspace_root / requested).resolve()
    # TODO: DeepAgents 的虚拟路径校验拒绝 Windows 盘符；extended path 可保留真实主机路径语义。
    if os.name == "nt" and resolved.drive:
        if resolved.drive.startswith("\\\\"):
            return "//?/UNC/" + resolved.as_posix().lstrip("/")
        return "//?/" + resolved.as_posix()
    return str(resolved)


def _read_implementations(
    middleware: FilesystemMiddleware,
) -> tuple[
    Callable[..., ToolMessage],
    Callable[..., Awaitable[ToolMessage]],
]:
    read_tool = next(
        (candidate for candidate in middleware.tools if candidate.name == "read_file"),
        None,
    )
    if read_tool is None:
        raise RuntimeError("FilesystemMiddleware 缺少 read_file 工具")
    if not isinstance(read_tool, StructuredTool):
        raise TypeError(
            "FilesystemMiddleware read_file 类型无效: "
            f"{type(read_tool).__name__}"
        )
    if read_tool.func is None or read_tool.coroutine is None:
        raise RuntimeError("FilesystemMiddleware read_file 缺少同步或异步实现")
    return (
        cast("Callable[..., ToolMessage]", read_tool.func),
        cast("Callable[..., Awaitable[ToolMessage]]", read_tool.coroutine),
    )


def configure_codex_read_file_tool(
    middleware: FilesystemMiddleware,
    *,
    workspace_root: Path,
) -> None:
    """把 DeepAgents `read_file` 的模型接口调整为 Codex 风格。"""

    resolved_workspace_root = workspace_root.resolve()
    tool_index = next(
        (
            index
            for index, candidate in enumerate(middleware.tools)
            if candidate.name == "read_file"
        ),
        None,
    )
    if tool_index is None:
        raise RuntimeError("FilesystemMiddleware 缺少 read_file 工具")
    upstream_tool = middleware.tools[tool_index]
    workspace_sync, workspace_async = _read_implementations(middleware)
    host_middleware = FilesystemMiddleware(
        backend=FilesystemBackend(
            root_dir=resolved_workspace_root,
            virtual_mode=False,
        ),
        _permissions=middleware._permissions,
        tool_token_limit_before_evict=None,
    )
    host_sync, host_async = _read_implementations(host_middleware)

    # LangChain 的 StructuredTool 通过 inspect.signature 识别注入参数，不能解析
    # postponed annotations；此文件必须让 ToolRuntime 在函数签名中保持真实类型。
    def sync_read_file(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
        line_offset: int = 1,
        max_lines: int | None = None,
    ) -> ToolMessage:
        virtual_path = _is_virtual_read_path(path)
        read_path = path if virtual_path else _host_read_path(path, resolved_workspace_root)
        implementation = workspace_sync if virtual_path else host_sync
        return cast(
            "ToolMessage",
            implementation(
                file_path=read_path,
                runtime=runtime,
                offset=line_offset - 1,
                limit=max_lines or DEFAULT_MAX_LINES,
            ),
        )

    async def async_read_file(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
        line_offset: int = 1,
        max_lines: int | None = None,
    ) -> ToolMessage:
        virtual_path = _is_virtual_read_path(path)
        read_path = path if virtual_path else _host_read_path(path, resolved_workspace_root)
        implementation = workspace_async if virtual_path else host_async
        return cast(
            "ToolMessage",
            await implementation(
                file_path=read_path,
                runtime=runtime,
                offset=line_offset - 1,
                limit=max_lines or DEFAULT_MAX_LINES,
            ),
        )

    middleware.tools[tool_index] = StructuredTool.from_function(
        name="read_file",
        description=upstream_tool.description,
        func=sync_read_file,
        coroutine=async_read_file,
        infer_schema=False,
        args_schema=CodexReadFileSchema,
    )


__all__ = ["CodexReadFileSchema", "configure_codex_read_file_tool"]
