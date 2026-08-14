import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Literal, cast

from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemState
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolArg, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.agents.middleware_prompts import FILESYSTEM_TOOL_DESCRIPTIONS
from app.agents.workspace_tool_paths import (
    WorkspaceToolPathResolver,
    backend_virtual_to_workspace_relative,
)

DEFAULT_MAX_LINES = 2_000


class _ToolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceLsSchema(_ToolSchema):
    path: str = Field(
        description="Workspace-relative directory path. Use '.' for the workspace root."
    )
    runtime: Annotated[object, InjectedToolArg]


class WorkspaceReadFileSchema(_ToolSchema):
    path: str = Field(
        description="Workspace-relative file path. Do not start it with '/'."
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
    runtime: Annotated[object, InjectedToolArg]


class WorkspaceWriteFileSchema(_ToolSchema):
    file_path: str = Field(
        description="Workspace-relative destination path. Do not start it with '/'."
    )
    content: str = Field(description="Text content to write.")
    runtime: Annotated[object, InjectedToolArg]


class WorkspaceEditFileSchema(_ToolSchema):
    file_path: str = Field(
        description="Workspace-relative file path. Do not start it with '/'."
    )
    old_string: str = Field(description="Exact text to replace.")
    new_string: str = Field(description="Replacement text; must differ from old_string.")
    replace_all: bool = Field(
        default=False,
        description="Replace every occurrence instead of requiring a unique match.",
    )
    runtime: Annotated[object, InjectedToolArg]


class WorkspaceGlobSchema(_ToolSchema):
    pattern: str = Field(description="Glob pattern to match files.")
    path: str = Field(
        default=".",
        description="Workspace-relative base directory. Defaults to '.'.",
    )
    runtime: Annotated[object, InjectedToolArg]


class WorkspaceGrepSchema(_ToolSchema):
    pattern: str = Field(description="Literal text to search for.")
    path: str | None = Field(
        default=None,
        description="Workspace-relative directory. Omit to search from '.'.",
    )
    glob: str | None = Field(
        default=None,
        description="Optional glob pattern used to filter searched files.",
    )
    output_mode: Literal["files_with_matches", "content", "count"] = Field(
        default="files_with_matches",
        description="Output format for matching files.",
    )
    runtime: Annotated[object, InjectedToolArg]


def _tool_implementations(
    middleware: FilesystemMiddleware,
    tool_name: str,
) -> tuple[
    Callable[..., ToolMessage],
    Callable[..., Awaitable[ToolMessage]],
]:
    tool = next(
        (candidate for candidate in middleware.tools if candidate.name == tool_name),
        None,
    )
    if tool is None:
        raise RuntimeError(f"FilesystemMiddleware 缺少 {tool_name} 工具")
    if not isinstance(tool, StructuredTool):
        raise TypeError(
            f"FilesystemMiddleware {tool_name} 类型无效: {type(tool).__name__}"
        )
    if tool.func is None or tool.coroutine is None:
        raise RuntimeError(f"FilesystemMiddleware {tool_name} 缺少同步或异步实现")
    return (
        cast("Callable[..., ToolMessage]", tool.func),
        cast("Callable[..., Awaitable[ToolMessage]]", tool.coroutine),
    )


def _path_error(
    tool_name: str,
    runtime: ToolRuntime[None, FilesystemState],
    error: ValueError,
) -> ToolMessage:
    return ToolMessage(
        content=f"Error: {error}",
        name=tool_name,
        tool_call_id=runtime.tool_call_id,
        status="error",
    )


def _rewrite_known_path(
    message: ToolMessage,
    *,
    virtual_path: str,
    relative_path: str,
) -> ToolMessage:
    content = message.content
    if isinstance(content, str):
        content = content.replace(virtual_path, relative_path)
    additional_kwargs = dict(message.additional_kwargs)
    read_path = additional_kwargs.get("read_file_path")
    if isinstance(read_path, str) and read_path.startswith("/"):
        additional_kwargs["read_file_path"] = backend_virtual_to_workspace_relative(
            read_path
        )
    return message.model_copy(
        update={"content": content, "additional_kwargs": additional_kwargs}
    )


def _rewrite_path_list(message: ToolMessage) -> ToolMessage:
    if message.status != "success" or not isinstance(message.content, str):
        return message
    try:
        paths = ast.literal_eval(message.content)
    except (SyntaxError, ValueError):
        return message
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return message
    relative_paths = [
        backend_virtual_to_workspace_relative(path) if path.startswith("/") else path
        for path in paths
    ]
    return message.model_copy(update={"content": str(relative_paths)})


def _rewrite_grep_paths(message: ToolMessage) -> ToolMessage:
    if message.status != "success" or not isinstance(message.content, str):
        return message
    lines = [line.removeprefix("/") for line in message.content.splitlines()]
    return message.model_copy(update={"content": "\n".join(lines)})


def configure_workspace_filesystem_tools(
    middleware: FilesystemMiddleware,
    *,
    workspace_root: Path,
) -> None:
    """将 DeepAgents 文件工具适配为模型可见的标准相对路径协议。"""

    resolver = WorkspaceToolPathResolver(workspace_root)
    implementations = {
        name: _tool_implementations(middleware, name)
        for name in ("ls", "read_file", "write_file", "edit_file", "glob", "grep")
    }

    def sync_ls(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
    ) -> ToolMessage:
        try:
            backend_path = resolver.backend_virtual_path(path)
        except ValueError as error:
            return _path_error("ls", runtime, error)
        return _rewrite_path_list(
            implementations["ls"][0](path=backend_path, runtime=runtime)
        )

    async def async_ls(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
    ) -> ToolMessage:
        try:
            backend_path = resolver.backend_virtual_path(path)
        except ValueError as error:
            return _path_error("ls", runtime, error)
        return _rewrite_path_list(
            await implementations["ls"][1](path=backend_path, runtime=runtime)
        )

    def sync_read_file(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
        line_offset: int = 1,
        max_lines: int | None = None,
    ) -> ToolMessage:
        try:
            relative_path = resolver.normalize_relative_path(path)
            backend_path = resolver.backend_virtual_path(path)
        except ValueError as error:
            return _path_error("read_file", runtime, error)
        result = implementations["read_file"][0](
            file_path=backend_path,
            runtime=runtime,
            offset=line_offset - 1,
            limit=max_lines or DEFAULT_MAX_LINES,
        )
        return _rewrite_known_path(
            result,
            virtual_path=backend_path,
            relative_path=relative_path,
        )

    async def async_read_file(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
        line_offset: int = 1,
        max_lines: int | None = None,
    ) -> ToolMessage:
        try:
            relative_path = resolver.normalize_relative_path(path)
            backend_path = resolver.backend_virtual_path(path)
        except ValueError as error:
            return _path_error("read_file", runtime, error)
        result = await implementations["read_file"][1](
            file_path=backend_path,
            runtime=runtime,
            offset=line_offset - 1,
            limit=max_lines or DEFAULT_MAX_LINES,
        )
        return _rewrite_known_path(
            result,
            virtual_path=backend_path,
            relative_path=relative_path,
        )

    def sync_write_file(
        file_path: str,
        content: str,
        runtime: ToolRuntime[None, FilesystemState],
    ) -> ToolMessage:
        try:
            relative_path = resolver.normalize_relative_path(file_path)
            backend_path = resolver.backend_virtual_path(file_path)
        except ValueError as error:
            return _path_error("write_file", runtime, error)
        result = implementations["write_file"][0](
            file_path=backend_path,
            content=content,
            runtime=runtime,
        )
        return _rewrite_known_path(
            result,
            virtual_path=backend_path,
            relative_path=relative_path,
        )

    async def async_write_file(
        file_path: str,
        content: str,
        runtime: ToolRuntime[None, FilesystemState],
    ) -> ToolMessage:
        try:
            relative_path = resolver.normalize_relative_path(file_path)
            backend_path = resolver.backend_virtual_path(file_path)
        except ValueError as error:
            return _path_error("write_file", runtime, error)
        result = await implementations["write_file"][1](
            file_path=backend_path,
            content=content,
            runtime=runtime,
        )
        return _rewrite_known_path(
            result,
            virtual_path=backend_path,
            relative_path=relative_path,
        )

    def sync_edit_file(
        file_path: str,
        old_string: str,
        new_string: str,
        runtime: ToolRuntime[None, FilesystemState],
        replace_all: bool = False,
    ) -> ToolMessage:
        try:
            relative_path = resolver.normalize_relative_path(file_path)
            backend_path = resolver.backend_virtual_path(file_path)
        except ValueError as error:
            return _path_error("edit_file", runtime, error)
        result = implementations["edit_file"][0](
            file_path=backend_path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
            runtime=runtime,
        )
        return _rewrite_known_path(
            result,
            virtual_path=backend_path,
            relative_path=relative_path,
        )

    async def async_edit_file(
        file_path: str,
        old_string: str,
        new_string: str,
        runtime: ToolRuntime[None, FilesystemState],
        replace_all: bool = False,
    ) -> ToolMessage:
        try:
            relative_path = resolver.normalize_relative_path(file_path)
            backend_path = resolver.backend_virtual_path(file_path)
        except ValueError as error:
            return _path_error("edit_file", runtime, error)
        result = await implementations["edit_file"][1](
            file_path=backend_path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
            runtime=runtime,
        )
        return _rewrite_known_path(
            result,
            virtual_path=backend_path,
            relative_path=relative_path,
        )

    def sync_glob(
        pattern: str,
        runtime: ToolRuntime[None, FilesystemState],
        path: str = ".",
    ) -> ToolMessage:
        try:
            backend_path = resolver.backend_virtual_path(path)
        except ValueError as error:
            return _path_error("glob", runtime, error)
        return _rewrite_path_list(
            implementations["glob"][0](
                pattern=pattern,
                path=backend_path,
                runtime=runtime,
            )
        )

    async def async_glob(
        pattern: str,
        runtime: ToolRuntime[None, FilesystemState],
        path: str = ".",
    ) -> ToolMessage:
        try:
            backend_path = resolver.backend_virtual_path(path)
        except ValueError as error:
            return _path_error("glob", runtime, error)
        return _rewrite_path_list(
            await implementations["glob"][1](
                pattern=pattern,
                path=backend_path,
                runtime=runtime,
            )
        )

    def sync_grep(
        pattern: str,
        runtime: ToolRuntime[None, FilesystemState],
        path: str | None = None,
        glob: str | None = None,
        output_mode: Literal[
            "files_with_matches", "content", "count"
        ] = "files_with_matches",
    ) -> ToolMessage:
        try:
            backend_path = resolver.backend_virtual_path(path or ".")
        except ValueError as error:
            return _path_error("grep", runtime, error)
        return _rewrite_grep_paths(
            implementations["grep"][0](
                pattern=pattern,
                path=backend_path,
                glob=glob,
                output_mode=output_mode,
                runtime=runtime,
            )
        )

    async def async_grep(
        pattern: str,
        runtime: ToolRuntime[None, FilesystemState],
        path: str | None = None,
        glob: str | None = None,
        output_mode: Literal[
            "files_with_matches", "content", "count"
        ] = "files_with_matches",
    ) -> ToolMessage:
        try:
            backend_path = resolver.backend_virtual_path(path or ".")
        except ValueError as error:
            return _path_error("grep", runtime, error)
        return _rewrite_grep_paths(
            await implementations["grep"][1](
                pattern=pattern,
                path=backend_path,
                glob=glob,
                output_mode=output_mode,
                runtime=runtime,
            )
        )

    replacements = {
        "ls": (sync_ls, async_ls, WorkspaceLsSchema),
        "read_file": (sync_read_file, async_read_file, WorkspaceReadFileSchema),
        "write_file": (sync_write_file, async_write_file, WorkspaceWriteFileSchema),
        "edit_file": (sync_edit_file, async_edit_file, WorkspaceEditFileSchema),
        "glob": (sync_glob, async_glob, WorkspaceGlobSchema),
        "grep": (sync_grep, async_grep, WorkspaceGrepSchema),
    }
    for index, tool in enumerate(middleware.tools):
        replacement = replacements.get(tool.name)
        if replacement is None:
            continue
        sync_implementation, async_implementation, schema = replacement
        middleware.tools[index] = StructuredTool.from_function(
            name=tool.name,
            description=FILESYSTEM_TOOL_DESCRIPTIONS[tool.name],
            func=sync_implementation,
            coroutine=async_implementation,
            infer_schema=False,
            args_schema=schema,
        )


__all__ = [
    "WorkspaceEditFileSchema",
    "WorkspaceGlobSchema",
    "WorkspaceGrepSchema",
    "WorkspaceLsSchema",
    "WorkspaceReadFileSchema",
    "WorkspaceWriteFileSchema",
    "configure_workspace_filesystem_tools",
]
