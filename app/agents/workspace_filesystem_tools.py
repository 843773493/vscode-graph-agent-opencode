import ast
import asyncio
import json
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, cast

from deepagents.backends.utils import format_grep_matches
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
DEFAULT_GREP_TIMEOUT_SECONDS = 10
GREP_TIMEOUT_GRACE_SECONDS = 1
GREP_MAX_FILE_SIZE = "10M"
GREP_MAX_MATCHES_PER_FILE = 256
GREP_MAX_MATCHES_TOTAL = 2_048
GREP_EXCLUDED_GLOBS = (
    "!.boxteam",
    "!.boxteam/**",
    "!.git",
    "!.git/**",
    "!node_modules",
    "!node_modules/**",
)
SYSTEM_SKILL_SOURCES = {
    "skills": "workspace",
    "bundled-skills": "bundled",
}


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
    additional_kwargs.update(_system_skill_metadata(relative_path))
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
    relative_paths = []
    for path in paths:
        relative_path = (
            backend_virtual_to_workspace_relative(path) if path.startswith("/") else path
        )
        if _is_hidden_runtime_path(relative_path):
            continue
        relative_paths.append(relative_path)
    return message.model_copy(update={"content": str(relative_paths)})


def _is_hidden_runtime_path(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").lstrip("/")
    return normalized == ".boxteam" or normalized.startswith(".boxteam/")


def _runtime_path_error(tool_name: str, relative_path: str) -> ValueError:
    return ValueError(
        f"{tool_name} 禁止访问工作区运行时目录: {relative_path!r}。"
        " .boxteam 由系统管理，不能用于普通源码搜索或读写；"
        "系统 Skill 只允许读取被注入的精确 SKILL.md 文件。"
    )


def _system_skill_metadata(relative_path: str) -> dict[str, str]:
    parts = PurePosixPath(relative_path).parts
    if len(parts) != 4 or parts[0] != ".boxteam" or parts[3] != "SKILL.md":
        return {}
    source = SYSTEM_SKILL_SOURCES.get(parts[1])
    if source is None:
        return {}
    return {
        "workspace_path_scope": "system_skill",
        "workspace_file_kind": "skill_definition",
        "skill_source": source,
        "skill_name": parts[2],
    }


def _validate_model_path(
    tool_name: str,
    relative_path: str,
    *,
    allow_skill_roots: bool = False,
) -> str:
    parts = PurePosixPath(relative_path).parts
    if not parts or parts[0] != ".boxteam":
        return relative_path
    if allow_skill_roots and _system_skill_metadata(relative_path):
        return relative_path
    raise _runtime_path_error(tool_name, relative_path)


def _validate_glob_pattern(pattern: str) -> None:
    normalized = pattern.replace("\\", "/")
    if ".boxteam" in PurePosixPath(normalized).parts or ".boxteam/" in normalized:
        raise _runtime_path_error("glob", pattern)


def _grep_scope_error(relative_path: str, glob: str | None) -> ValueError:
    scope = f"path={relative_path!r}"
    if glob is not None:
        scope += f", glob={glob!r}"
    return ValueError(
        "grep 拒绝无界搜索运行时或根目录范围（"
        f"{scope}）。.boxteam 包含会话日志、message stream 和 trace；"
        "请把 path 限定到源码目录（例如 parry_arena 或 src），"
        "并按需用 glob 分批搜索。"
    )


def _validate_grep_scope(
    resolver: WorkspaceToolPathResolver,
    path: str | None,
    glob: str | None,
) -> str:
    relative_path = resolver.normalize_relative_path(path or ".")
    if ".boxteam" in PurePosixPath(relative_path).parts:
        raise _grep_scope_error(relative_path, glob)
    if relative_path == "." and (glob is None or "**" in glob):
        raise _grep_scope_error(relative_path, glob)
    return relative_path


def _grep_result_error(
    runtime: ToolRuntime[None, FilesystemState],
    detail: str,
) -> ToolMessage:
    return ToolMessage(
        content=f"Error: {detail}",
        name="grep",
        tool_call_id=runtime.tool_call_id,
        status="error",
    )


def _run_bounded_workspace_grep(
    *,
    pattern: str,
    relative_path: str,
    glob: str | None,
    output_mode: Literal["files_with_matches", "content", "count"],
    workspace_root: Path,
    runtime: ToolRuntime[None, FilesystemState],
) -> ToolMessage:
    search_path = workspace_root if relative_path == "." else workspace_root / relative_path
    command = [
        "rg",
        "--json",
        "--fixed-strings",
        "--hidden",
        "--no-messages",
        "--max-filesize",
        GREP_MAX_FILE_SIZE,
    ]
    if glob:
        command.extend(("--glob", glob))
    for excluded_glob in GREP_EXCLUDED_GLOBS:
        command.extend(("--glob", excluded_glob))
    command.extend(
        (
            "--max-count",
            str(GREP_MAX_MATCHES_PER_FILE),
            "--",
            pattern,
            str(search_path),
        )
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DEFAULT_GREP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _grep_result_error(
            runtime,
            "grep 在 "
            f"{DEFAULT_GREP_TIMEOUT_SECONDS} 秒内未完成，已终止搜索；"
            f"path={relative_path!r}, glob={glob!r}。请缩小 path 或 glob 后重试。",
        )
    except FileNotFoundError:
        return _grep_result_error(
            runtime,
            "grep 需要可用的 rg 命令；当前运行环境未找到 rg，"
            "未执行无界的备用递归扫描。",
        )
    except OSError as error:
        return _grep_result_error(runtime, f"grep 启动失败: {error}")

    if completed.returncode not in {0, 1}:
        detail = completed.stderr.strip() or f"rg 退出码为 {completed.returncode}"
        return _grep_result_error(runtime, f"grep 执行失败: {detail}")

    matches: list[dict[str, str | int]] = []
    truncated = False
    resolved_root = workspace_root.resolve()
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        path_data = data.get("path")
        lines_data = data.get("lines")
        raw_path = path_data.get("text") if isinstance(path_data, dict) else None
        line_number = data.get("line_number")
        line_text = lines_data.get("text", "") if isinstance(lines_data, dict) else ""
        if (
            not isinstance(raw_path, str)
            or not isinstance(line_number, int)
            or not isinstance(line_text, str)
        ):
            continue
        try:
            match_path = Path(raw_path).resolve()
            match_relative = match_path.relative_to(resolved_root).as_posix()
        except (OSError, RuntimeError, ValueError):
            continue
        if ".boxteam" in PurePosixPath(match_relative).parts:
            continue
        if len(matches) >= GREP_MAX_MATCHES_TOTAL:
            truncated = True
            continue
        matches.append(
            {
                "path": match_relative,
                "line": line_number,
                "text": line_text.rstrip("\n"),
            }
        )

    content = format_grep_matches(matches, output_mode)
    if truncated:
        content += (
            "\n[grep 结果已限制为 "
            f"{GREP_MAX_MATCHES_TOTAL} 条；请缩小 path 或 glob 后继续搜索]"
        )
    return ToolMessage(
        content=content,
        name="grep",
        tool_call_id=runtime.tool_call_id,
        status="success",
    )


def configure_workspace_filesystem_tools(
    middleware: FilesystemMiddleware,
    *,
    workspace_root: Path,
) -> None:
    """将 DeepAgents 文件工具适配为模型可见的标准相对路径协议。"""

    resolver = WorkspaceToolPathResolver(workspace_root)
    implementations = {
        name: _tool_implementations(middleware, name)
        for name in ("ls", "read_file", "write_file", "edit_file", "glob")
    }

    def sync_ls(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
    ) -> ToolMessage:
        try:
            relative_path = resolver.normalize_relative_path(path)
            _validate_model_path("ls", relative_path)
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
            relative_path = resolver.normalize_relative_path(path)
            _validate_model_path("ls", relative_path)
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
            _validate_model_path("read_file", relative_path, allow_skill_roots=True)
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
            _validate_model_path("read_file", relative_path, allow_skill_roots=True)
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
            _validate_model_path("write_file", relative_path)
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
            _validate_model_path("write_file", relative_path)
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
            _validate_model_path("edit_file", relative_path)
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
            _validate_model_path("edit_file", relative_path)
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
            _validate_glob_pattern(pattern)
            relative_path = resolver.normalize_relative_path(path)
            _validate_model_path("glob", relative_path)
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
            _validate_glob_pattern(pattern)
            relative_path = resolver.normalize_relative_path(path)
            _validate_model_path("glob", relative_path)
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
            relative_path = _validate_grep_scope(resolver, path, glob)
        except ValueError as error:
            return _path_error("grep", runtime, error)
        return _run_bounded_workspace_grep(
            pattern=pattern,
            relative_path=relative_path,
            glob=glob,
            output_mode=output_mode,
            workspace_root=resolver.workspace_root,
            runtime=runtime,
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
            relative_path = _validate_grep_scope(resolver, path, glob)
        except ValueError as error:
            return _path_error("grep", runtime, error)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _run_bounded_workspace_grep,
                    pattern=pattern,
                    relative_path=relative_path,
                    glob=glob,
                    output_mode=output_mode,
                    workspace_root=resolver.workspace_root,
                    runtime=runtime,
                ),
                timeout=DEFAULT_GREP_TIMEOUT_SECONDS + GREP_TIMEOUT_GRACE_SECONDS,
            )
        except TimeoutError:
            return _grep_result_error(
                runtime,
                "grep 调度层在 "
                f"{DEFAULT_GREP_TIMEOUT_SECONDS + GREP_TIMEOUT_GRACE_SECONDS} 秒内未完成，"
                "已取消本次工具结果；请缩小 path 或 glob 后重试。",
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
