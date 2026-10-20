import asyncio
import base64
import json
import logging
import mimetypes
import time
from functools import partial, update_wrapper
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import httpx
from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import FileData, ReadResult
from deepagents.backends.utils import (
    _get_file_type,
    check_empty_content,
    format_content_with_line_numbers,
    format_grep_matches,
    slice_read_response,
)
from deepagents.middleware.filesystem import (
    FilesystemMiddleware,
    FilesystemState,
    _check_fs_permission,
)
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolArg, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.agents.middleware_prompts import FILESYSTEM_TOOL_DESCRIPTIONS
from app.agents.workspace_backend import SESSION_ARTIFACT_ROUTE, SessionArtifactBackend
from app.agents.workspace_tool_paths import (
    WorkspaceToolPathResolver,
)
from app.core.resource_uri import ResourceUriGrammarError, parse_resource_uri
from app.core.trace_middleware import require_current_gateway_id
from app.gateway.federation.errors import FederationError
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.schemas.internal_v2.text_mutation import CreateText, EditText, TextMutation
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.workspace_files.text_mutation import (
    text_mutation_failure,
)
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)

GrepOutputMode = Literal["files_with_matches", "content", "count"]
logger = logging.getLogger(__name__)
DEFAULT_MAX_LINES = 2_000
class _ToolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceLsSchema(_ToolSchema):
    path: str = Field(
        description=(
            "Directory target: a workspace-relative/native path or resource URI; use '.' for the workspace root."
        )
    )
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    timeout_ms: int = Field(default=10000, ge=1, le=30000)
    scan_limit: int = Field(default=10000, ge=1, le=100000)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=1048576)
    runtime: Annotated[object, InjectedToolArg]


class WorkspaceReadFileSchema(_ToolSchema):
    path: str = Field(
        description="File target: a workspace-relative/native path or a resource URI returned by ls."
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
    max_bytes: int = Field(default=16777216, ge=1, le=33554432)
    max_response_bytes: int = Field(default=25165824, ge=1024, le=50331648)
    timeout_ms: int = Field(default=10000, ge=1, le=30000)
    runtime: Annotated[object, InjectedToolArg]


class WorkspaceWriteFileSchema(_ToolSchema):
    file_path: str = Field(
        description="Destination file path; prefer a workspace-relative path."
    )
    content: str = Field(description="Text content to write.")
    expected_version: Literal["absent"] = Field(description="Explicitly require the destination to be absent.")
    runtime: Annotated[object, InjectedToolArg]


class WorkspaceEditFileSchema(_ToolSchema):
    file_path: str = Field(
        description="File path to edit; prefer a workspace-relative path."
    )
    expected_version: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", description="Raw-byte version from the previous read_file result.")
    old_string: str = Field(description="Exact text to replace.")
    new_string: str = Field(description="Replacement text; must differ from old_string.")
    replace_all: bool = Field(
        default=False,
        description="Replace every occurrence instead of requiring a unique match.",
    )
    runtime: Annotated[object, InjectedToolArg]


class _SearchSchema(_ToolSchema):
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    timeout_ms: int = Field(default=10000, ge=1, le=30000)
    scan_limit: int = Field(default=10000, ge=1, le=100000)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=1048576)
    runtime: Annotated[object, InjectedToolArg]


class WorkspaceGlobSchema(_SearchSchema):
    pattern: str = Field(description="Glob pattern to match files.")
    path: str = Field(
        default=".",
        description="Base directory; prefer a workspace-relative path and defaults to '.'.",
    )


class WorkspaceGrepSchema(_SearchSchema):
    pattern: str = Field(description="Literal text to search for.")
    path: str | None = Field(
        default=None,
        description="Directory to search; prefer a workspace-relative path and omit to search from '.'.",
    )
    glob: str | None = Field(
        default=None,
        description="Optional glob pattern used to filter searched files.",
    )
    output_mode: GrepOutputMode = Field(
        default="files_with_matches",
        description="Output format for matching files.",
    )
    max_bytes: int = Field(default=10485760, ge=1, le=33554432)
    max_snippet_bytes: int = Field(default=2048, ge=16, le=8192)


def _path_error(
    tool_name: str,
    runtime: ToolRuntime[None, FilesystemState],
    error: OSError | RuntimeError | ValueError | httpx.HTTPError | FederationError,
) -> ToolMessage:
    if isinstance(error, ResourceFileOperationError):
        content = json.dumps(error.detail(), ensure_ascii=False)
    elif isinstance(error, FederationError):
        content = json.dumps({"owner": "federation", **error.to_wire()}, ensure_ascii=False)
    elif isinstance(error, (ResourceUriGrammarError, ResourceUriResolveError)):
        content = json.dumps({
            "owner": "resource_uri_grammar" if isinstance(error, ResourceUriGrammarError) else "resource_uri_resolver",
            "code": error.reason_code, "message": str(error),
        }, ensure_ascii=False)
    elif tool_name in {"ls", "read_file", "glob", "grep"} and isinstance(error, (OSError, httpx.HTTPError)):
        logger.error("文件导航执行失败", exc_info=error)
        content = json.dumps({
            "owner": "resource_file_operations", "code": "execution_failed",
            "reason": "filesystem" if isinstance(error, OSError) else "transport",
            "type": type(error).__name__, "errno": getattr(error, "errno", None),
            "message": error.strerror if isinstance(error, OSError) else "文件导航传输失败",
        }, ensure_ascii=False)
    else:
        content = f"Error: {error}"
    return ToolMessage(
        content=content,
        name=tool_name,
        tool_call_id=runtime.tool_call_id,
        status="error",
    )


def _runtime_path_error(tool_name: str, relative_path: str) -> ValueError:
    return ValueError(
        f"{tool_name} 禁止访问工作区运行时目录: {relative_path!r}。"
        " .boxteam 由系统管理，不能用于普通源码搜索或读写；"
        "Skill 文件应通过已公布的资源 URI 读取；激活使用 skill_load。"
    )


def _validate_model_path(
    tool_name: str,
    relative_path: str,
) -> str:
    parts = PurePosixPath(relative_path).parts
    if len(parts) >= 3 and parts[0] == "session-artifacts" and parts[2] == "attachments" and tool_name != "read_file":
        raise ValueError(f"{tool_name} 不提供附件写入或枚举 capability")
    if not parts or parts[0] != ".boxteam":
        return relative_path
    raise _runtime_path_error(tool_name, relative_path)


def _read_attachment_message(
    backend: SessionArtifactBackend, relative_path: str,
    runtime: ToolRuntime[None, FilesystemState], line_offset: int, max_lines: int,
) -> ToolMessage:
    """逻辑附件按 catalog MIME 投影；文本分页复用 DeepAgents 文件格式化。"""
    try:
        content = backend.read_attachment(relative_path.removeprefix("session-artifacts/"))
        mime_type = content.content_type
        if mime_type.startswith("text/") or mime_type in {"application/json", "application/xml", "application/javascript"}:
            sliced = slice_read_response(FileData(content=content.data.decode("utf-8"), encoding="utf-8"), line_offset - 1, max_lines)
            if isinstance(sliced, ReadResult):
                return _path_error("read_file", runtime, ValueError(sliced.error))
            return ToolMessage(content=format_content_with_line_numbers(sliced, start_line=line_offset), name="read_file", tool_call_id=runtime.tool_call_id, status="success")
        media_type = mime_type.split("/", 1)[0]
        return ToolMessage(
            content_blocks=[{"type": media_type if media_type in {"image", "audio", "video"} else "file", "base64": base64.b64encode(content.data).decode("ascii"), "mime_type": mime_type}],
            name="read_file", tool_call_id=runtime.tool_call_id, status="success",
            additional_kwargs={"read_file_path": relative_path, "read_file_media_type": mime_type},
        )
    except (OSError, RuntimeError, ValueError) as error:
        return _path_error("read_file", runtime, error)


def _read_navigation_message(
    result: ResourceNavigationResultDTO, runtime: ToolRuntime[None, FilesystemState], max_response_bytes: int,
) -> ToolMessage:
    """普通文件 owner 的读取结果投影为既有模型文本或媒体合同。"""
    if result.content is None:
        raise ValueError("文件 owner 未返回读取内容")
    metadata = {"resource_navigation": result.model_dump(exclude={"content", "entries"})}
    if result.content_encoding == "base64":
        message = ToolMessage(
            content_blocks=[{"type": result.media_type, "base64": result.content, "mime_type": result.mime_type}],
            name="read_file", tool_call_id=runtime.tool_call_id, status="success",
            additional_kwargs={**metadata, "read_file_path": result.node.uri, "read_file_media_type": result.mime_type},
        )
        if len(message.model_dump_json().encode()) > max_response_bytes:
            raise ResourceFileOperationError("output_limit_exceeded", "媒体工具结果超过响应字节上限", reason="response_byte_limit")
        return message
    content = (check_empty_content(result.content) if not result.truncated else None) or format_content_with_line_numbers(
        result.content, start_line=result.line_offset or 1,
    )
    def message_for(text: str, reason: str | None) -> ToolMessage:
        if result.content_hash is not None:
            text += f"\n[原文件字节版本：{result.content_hash}]"
        if reason is not None:
            text += f"\n[内容已截断：{reason}；目标 {result.node.uri}。按行继续或显式增加字节预算。]"
        return ToolMessage(content=text, name="read_file", tool_call_id=runtime.tool_call_id,
                           status="success", additional_kwargs=metadata)

    message = message_for(content, result.truncation_reason)
    if len(message.model_dump_json().encode()) <= max_response_bytes:
        return message
    metadata["resource_navigation"].update(truncated=True, truncation_reason="response_byte_limit")
    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        if len(message_for(content[:middle], "response_byte_limit").model_dump_json().encode()) <= max_response_bytes:
            low = middle
        else:
            high = middle - 1
    message = message_for(content[:low], "response_byte_limit")
    if len(message.model_dump_json().encode()) > max_response_bytes:
        raise ResourceFileOperationError("output_limit_exceeded", "响应预算不足以容纳读取回执", reason="response_byte_limit")
    return message


def _validate_glob_pattern(pattern: str) -> None:
    normalized = pattern.replace("\\", "/")
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError("glob 模式不能包含越界路径段")
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


def _run_session_artifact_grep(
    *,
    pattern: str,
    relative_path: str,
    glob: str | None,
    output_mode: GrepOutputMode,
    backend: SessionArtifactBackend,
    runtime: ToolRuntime[None, FilesystemState],
) -> ToolMessage:
    """在模型可见的会话产物虚拟路径中搜索。"""

    artifact_tail = relative_path.removeprefix("session-artifacts").lstrip("/")
    backend_path = "/" if not artifact_tail else f"/{artifact_tail}"
    result = backend.grep(pattern, path=backend_path, glob=glob)
    if result.error:
        return _grep_result_error(runtime, result.error)

    matches: list[dict[str, str | int]] = []
    for match in result.matches or []:
        raw_path = match.get("path")
        line_number = match.get("line")
        line_text = match.get("text")
        if (
            not isinstance(raw_path, str)
            or not isinstance(line_number, int)
            or not isinstance(line_text, str)
        ):
            continue
        matches.append(
            {
                "path": f"session-artifacts/{raw_path.lstrip('/')}",
                "line": line_number,
                "text": line_text,
            }
        )
    return ToolMessage(
        content=format_grep_matches(matches, output_mode),
        name="grep",
        tool_call_id=runtime.tool_call_id,
        status="success",
    )


def configure_workspace_filesystem_tools(
    middleware: FilesystemMiddleware,
    *,
    workspace_root: Path,
    resource_navigation: ResourceNavigation,
    text_mutations: WorkspaceTextMutationOwner,
) -> None:
    """普通目录和文件读取交共享 owner；产物继续交其独立业务 owner。"""

    resolver = WorkspaceToolPathResolver(workspace_root)
    session_artifact_backend = (
        middleware.backend.routes[SESSION_ARTIFACT_ROUTE]
        if isinstance(middleware.backend, CompositeBackend)
        else SessionArtifactBackend(workspace_root)
    )
    if not isinstance(session_artifact_backend, SessionArtifactBackend):
        raise TypeError("Session artifacts route 必须由唯一 SessionArtifactBackend 装配")

    def resolve_paths(
        tool_name: str,
        path: str,
    ) -> tuple[str, str]:
        """统一模型相对路径与后端虚拟路径的规范化。

        顺序固定为：先规范化为模型可见相对路径（越界、非法路径在此抛
        ValueError），再做模型路径准入校验，最后投影为后端虚拟路径；
        与各工具原有实现逐字一致。
        """
        relative_path = resolver.normalize_relative_path(path)
        _validate_model_path(tool_name, relative_path)
        return relative_path, "/" if relative_path == "." else f"/{relative_path}"

    def sync_ls(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
        limit: int = 100, cursor: str | None = None, timeout_ms: int = 10000,
        scan_limit: int = 10000, max_response_bytes: int = 1048576,
    ) -> ToolMessage:
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            gateway_id = require_current_gateway_id("ls")
            uri = resource_navigation.input_uri(path)
            if time.monotonic() >= deadline:
                raise TimeoutError
            result = resource_navigation.navigate_sync(ResourceNavigationRequest(
                uri=uri, operation="list", limit=limit, cursor=cursor,
                timeout_ms=timeout_ms, scan_limit=scan_limit, max_response_bytes=max_response_bytes,
            ), local_gateway_id=gateway_id)
            if time.monotonic() >= deadline:
                raise ResourceFileOperationError("output_limit_exceeded", "目录导航超过本次总期限", reason="deadline")
        except (TimeoutError, httpx.TimeoutException):
            return _path_error("ls", runtime, ResourceFileOperationError(
                "output_limit_exceeded", "目录导航超过本次总期限", reason="deadline",
            ))
        except (OSError, RuntimeError, ValueError, httpx.HTTPError, FederationError) as error:
            return _path_error("ls", runtime, error)
        return ToolMessage(
            content=result.model_dump_json(), name="ls", tool_call_id=runtime.tool_call_id, status="success",
        )

    async def async_ls(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
        limit: int = 100, cursor: str | None = None, timeout_ms: int = 10000,
        scan_limit: int = 10000, max_response_bytes: int = 1048576,
    ) -> ToolMessage:
        try:
            gateway_id = require_current_gateway_id("ls")
            async with asyncio.timeout(timeout_ms / 1000):
                uri = await asyncio.to_thread(resource_navigation.input_uri, path)
                result = await resource_navigation.navigate(ResourceNavigationRequest(
                    uri=uri, operation="list", limit=limit, cursor=cursor,
                    timeout_ms=timeout_ms, scan_limit=scan_limit, max_response_bytes=max_response_bytes,
                ), local_gateway_id=gateway_id)
        except (TimeoutError, httpx.TimeoutException):
            return _path_error("ls", runtime, ResourceFileOperationError(
                "output_limit_exceeded", "目录导航超过本次总期限", reason="deadline",
            ))
        except (OSError, RuntimeError, ValueError, httpx.HTTPError, FederationError) as error:
            return _path_error("ls", runtime, error)
        return ToolMessage(
            content=result.model_dump_json(), name="ls", tool_call_id=runtime.tool_call_id, status="success",
        )

    def sync_read_file(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
        line_offset: int = 1,
        max_lines: int | None = None,
        max_bytes: int = 16777216, max_response_bytes: int = 25165824, timeout_ms: int = 10000,
    ) -> ToolMessage:
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            artifact = artifact_read(path, runtime, line_offset, max_lines)
            if artifact is not None:
                return artifact
            gateway_id = require_current_gateway_id("read_file")
            uri = resource_navigation.input_uri(path)
            require_read_permission(uri)
            result = resource_navigation.navigate_sync(ResourceNavigationRequest(
                uri=uri, operation="read", read_format="auto", line_offset=line_offset,
                max_lines=max_lines or DEFAULT_MAX_LINES, max_bytes=max_bytes,
                max_response_bytes=max_response_bytes, timeout_ms=timeout_ms,
            ), local_gateway_id=gateway_id)
            message = _read_navigation_message(result, runtime, max_response_bytes)
            if time.monotonic() >= deadline:
                raise TimeoutError
            return message
        except (TimeoutError, httpx.TimeoutException):
            return _path_error("read_file", runtime, ResourceFileOperationError(
                "output_limit_exceeded", "文件读取超过本次总期限", reason="deadline",
            ))
        except (OSError, RuntimeError, ValueError, httpx.HTTPError, FederationError) as error:
            return _path_error("read_file", runtime, error)

    def artifact_read(
        path: str, runtime: ToolRuntime[None, FilesystemState], line_offset: int, max_lines: int | None,
    ) -> ToolMessage | None:
        # TODO（第5组）：全部产物 URI 生成/消费同批迁齐后删除此旧地址入口。
        if not (path.startswith(("boxteam-session://", "session-artifacts/")) or path == "session-artifacts"):
            return None
        relative_path, backend_path = resolve_paths("read_file", path)
        parts = PurePosixPath(relative_path).parts
        if len(parts) >= 3 and parts[2] == "attachments":
            return _read_attachment_message(session_artifact_backend, relative_path, runtime, line_offset, max_lines or DEFAULT_MAX_LINES)
        result = session_artifact_backend.read(backend_path.removeprefix("/session-artifacts"),
                                              offset=line_offset - 1, limit=max_lines or DEFAULT_MAX_LINES)
        if result.error or result.file_data is None:
            raise ValueError(result.error or "产物 owner 未返回读取内容")
        if result.file_data["encoding"] == "base64":
            mime_type = mimetypes.guess_type("file" + Path(relative_path).suffix)[0] or "application/octet-stream"
            return ToolMessage(content_blocks=[{"type": _get_file_type(relative_path),
                "base64": result.file_data["content"], "mime_type": mime_type}], name="read_file",
                tool_call_id=runtime.tool_call_id, status="success",
                additional_kwargs={"read_file_path": relative_path, "read_file_media_type": mime_type})
        return ToolMessage(content=check_empty_content(result.file_data["content"]) or format_content_with_line_numbers(
            result.file_data["content"], start_line=line_offset,
        ), name="read_file", tool_call_id=runtime.tool_call_id, status="success")

    def require_read_permission(uri: str) -> None:
        parsed = parse_resource_uri(uri)
        permission_path = "/" + "/".join(parsed.tail_segments[1:]) if parsed.scope == "workspace" and parsed.kind == "files" else uri
        if _check_fs_permission(middleware._permissions, "read", permission_path) == "deny":
            raise ResourceUriResolveError("capability_denied", "文件目标被当前工具读取规则拒绝")

    async def async_read_file(
        path: str,
        runtime: ToolRuntime[None, FilesystemState],
        line_offset: int = 1,
        max_lines: int | None = None,
        max_bytes: int = 16777216, max_response_bytes: int = 25165824, timeout_ms: int = 10000,
    ) -> ToolMessage:
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            gateway_id = require_current_gateway_id("read_file")
            async with asyncio.timeout(timeout_ms / 1000):
                artifact = await asyncio.to_thread(artifact_read, path, runtime, line_offset, max_lines)
                if artifact is not None:
                    return artifact
                uri = await asyncio.to_thread(resource_navigation.input_uri, path)
                require_read_permission(uri)
                result = await resource_navigation.navigate(ResourceNavigationRequest(
                    uri=uri, operation="read", read_format="auto", line_offset=line_offset,
                    max_lines=max_lines or DEFAULT_MAX_LINES, max_bytes=max_bytes,
                    max_response_bytes=max_response_bytes, timeout_ms=timeout_ms,
                ), local_gateway_id=gateway_id)
                message = _read_navigation_message(result, runtime, max_response_bytes)
                if time.monotonic() >= deadline:
                    raise TimeoutError
                return message
        except (TimeoutError, httpx.TimeoutException):
            return _path_error("read_file", runtime, ResourceFileOperationError(
                "output_limit_exceeded", "文件读取超过本次总期限", reason="deadline",
            ))
        except (OSError, RuntimeError, ValueError, httpx.HTTPError, FederationError) as error:
            return _path_error("read_file", runtime, error)

    def authorize_write(uri: str) -> None:
        parsed = parse_resource_uri(uri)
        relative = "/".join(parsed.tail_segments[1:])
        _validate_model_path("write_file", relative)
        if _check_fs_permission(middleware._permissions, "write", "/" + relative) == "deny":
            raise ResourceUriResolveError("capability_denied", "文件目标被当前工具写入规则拒绝")

    def mutation_message(tool_name: str, result: dict[str, object], runtime: ToolRuntime[None, FilesystemState]) -> ToolMessage:
        return ToolMessage(content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                           name=tool_name, tool_call_id=runtime.tool_call_id, status=result["status"])

    def mutate_file(tool_name: str, request: TextMutation, runtime: ToolRuntime[None, FilesystemState]) -> ToolMessage:
        try:
            result = text_mutations.execute(request, authorize=authorize_write)
        except (OSError, RuntimeError, ValueError) as error:
            result = text_mutation_failure(error)
        return mutation_message(tool_name, result, runtime)

    async def mutate_file_async(tool_name: str, request: TextMutation, runtime: ToolRuntime[None, FilesystemState]) -> ToolMessage:
        try:
            result = await text_mutations.execute_async(request, authorize=authorize_write)
        except (OSError, RuntimeError, ValueError) as error:
            result = text_mutation_failure(error)
        return mutation_message(tool_name, result, runtime)

    def sync_write_file(file_path: str, content: str, expected_version: str,
                        runtime: ToolRuntime[None, FilesystemState]) -> ToolMessage:
        return mutate_file("write_file", CreateText(target=file_path, content=content, expected_version=expected_version), runtime)

    async def async_write_file(file_path: str, content: str, expected_version: str,
                               runtime: ToolRuntime[None, FilesystemState]) -> ToolMessage:
        return await mutate_file_async("write_file", CreateText(target=file_path, content=content, expected_version=expected_version), runtime)

    def sync_edit_file(file_path: str, old_string: str, new_string: str, expected_version: str,
                       runtime: ToolRuntime[None, FilesystemState], replace_all: bool = False) -> ToolMessage:
        return mutate_file("edit_file", EditText(target=file_path, old_string=old_string, new_string=new_string,
            expected_version=expected_version, replace_all=replace_all), runtime)

    async def async_edit_file(file_path: str, old_string: str, new_string: str, expected_version: str,
                              runtime: ToolRuntime[None, FilesystemState], replace_all: bool = False) -> ToolMessage:
        return await mutate_file_async("edit_file", EditText(target=file_path, old_string=old_string, new_string=new_string,
            expected_version=expected_version, replace_all=replace_all), runtime)

    def search_request(
        tool_name: str, pattern: str, path: str | None, glob: str | None,
        limit: int, cursor: str | None, timeout_ms: int, scan_limit: int,
        max_response_bytes: int, max_bytes: int, max_snippet_bytes: int,
    ) -> ResourceNavigationRequest:
        if tool_name == "glob":
            _validate_glob_pattern(pattern)
        uri = resource_navigation.input_uri("." if path is None else path)
        target = parse_resource_uri(uri)
        if tool_name == "grep" and target.kind == "files" and (
            ".boxteam" in target.tail_segments[1:] or
            (target.scope == "workspace" and len(target.tail_segments) == 1 and (glob is None or "**" in glob))
        ):
            raise _grep_scope_error(uri, glob)
        require_read_permission(uri)
        return ResourceNavigationRequest(uri=uri, operation="search", query=pattern,
            search_mode="glob" if tool_name == "glob" else "content", file_glob=glob,
            limit=limit, cursor=cursor, timeout_ms=timeout_ms, scan_limit=scan_limit,
            max_response_bytes=max_response_bytes, max_bytes=max_bytes, max_snippet_bytes=max_snippet_bytes)

    def search_message(
        tool_name: str, result: ResourceNavigationResultDTO, output_mode: GrepOutputMode,
        runtime: ToolRuntime[None, FilesystemState], max_response_bytes: int,
    ) -> ToolMessage:
        entries = []
        for entry in result.entries:
            parsed = parse_resource_uri(entry.uri)
            permission_path = "/" + "/".join(parsed.tail_segments[1:]) if parsed.scope == "workspace" and parsed.kind == "files" else entry.uri
            if _check_fs_permission(middleware._permissions, "read", permission_path) != "deny":
                entries.append(entry)
        allowed = {entry.uri for entry in entries}
        result = result.model_copy(update={"entries": tuple(entries),
            "matches": tuple(match for match in result.matches if match.uri in allowed)})
        if tool_name == "glob":
            message = ToolMessage(content=result.model_dump_json(), name=tool_name,
                tool_call_id=runtime.tool_call_id, status="success")
        else:
            matches = [{"path": match.uri, "line": match.line,
                        "text": match.text + (" [片段已截断]" if match.truncated else "")} for match in result.matches]
            metadata = result.model_dump(exclude={"entries", "matches", "content"})
            content = format_grep_matches(matches, output_mode)
            content += "\n" + json.dumps(metadata, ensure_ascii=False)
            message = ToolMessage(content=content, name=tool_name, tool_call_id=runtime.tool_call_id,
                                  status="success", additional_kwargs={"resource_navigation": metadata})
        if len(message.model_dump_json().encode()) > max_response_bytes:
            raise ResourceFileOperationError("output_limit_exceeded", "搜索工具回执超过字节预算，请减少 limit 或片段长度", reason="response_byte_limit")
        return message

    def artifact_search(
        tool_name: str, pattern: str, path: str | None, glob: str | None,
        output_mode: GrepOutputMode, runtime: ToolRuntime[None, FilesystemState],
    ) -> ToolMessage | None:
        # TODO（第5组）：产物生成/消费迁齐后删除旧目录地址，不开放 Session 私有磁盘树。
        if path is None or not (path == "session-artifacts" or path.startswith(("session-artifacts/", "boxteam-session://"))):
            return None
        relative_path, backend_path = resolve_paths(tool_name, path)
        if tool_name == "grep":
            return _run_session_artifact_grep(pattern=pattern, relative_path=relative_path, glob=glob,
                output_mode=output_mode, backend=session_artifact_backend, runtime=runtime)
        _validate_glob_pattern(pattern)
        result = session_artifact_backend.glob(pattern, path=backend_path.removeprefix("/session-artifacts") or "/")
        if result.error:
            raise ValueError(result.error)
        paths = ["session-artifacts/" + item["path"].lstrip("/") for item in result.matches or []]
        return ToolMessage(content=str(paths), name=tool_name, tool_call_id=runtime.tool_call_id, status="success")

    def sync_search(
        pattern: str, runtime: ToolRuntime[None, FilesystemState], path: str | None = None,
        glob: str | None = None, output_mode: GrepOutputMode = "files_with_matches",
        limit: int = 100, cursor: str | None = None, timeout_ms: int = 10000, scan_limit: int = 10000,
        max_response_bytes: int = 1048576, max_bytes: int = 10485760, max_snippet_bytes: int = 2048,
        *, tool_name: str,
    ) -> ToolMessage:
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            artifact = artifact_search(tool_name, pattern, path, glob, output_mode, runtime)
            if artifact is not None:
                return artifact
            gateway_id = require_current_gateway_id(tool_name)
            request = search_request(tool_name, pattern, path, glob, limit, cursor, timeout_ms, scan_limit,
                                     max_response_bytes, max_bytes, max_snippet_bytes)
            if time.monotonic() >= deadline:
                raise TimeoutError
            result = resource_navigation.navigate_sync(request, local_gateway_id=gateway_id)
            message = search_message(tool_name, result, output_mode, runtime, max_response_bytes)
            if time.monotonic() >= deadline:
                raise TimeoutError
            return message
        except (TimeoutError, httpx.TimeoutException):
            return _path_error(tool_name, runtime, ResourceFileOperationError(
                "output_limit_exceeded", "文件搜索超过本次总期限", reason="deadline"))
        except (OSError, RuntimeError, ValueError, httpx.HTTPError, FederationError) as error:
            return _path_error(tool_name, runtime, error)

    async def async_search(
        pattern: str, runtime: ToolRuntime[None, FilesystemState], path: str | None = None,
        glob: str | None = None, output_mode: GrepOutputMode = "files_with_matches",
        limit: int = 100, cursor: str | None = None, timeout_ms: int = 10000, scan_limit: int = 10000,
        max_response_bytes: int = 1048576, max_bytes: int = 10485760, max_snippet_bytes: int = 2048,
        *, tool_name: str,
    ) -> ToolMessage:
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            gateway_id = require_current_gateway_id(tool_name)
            async with asyncio.timeout(timeout_ms / 1000):
                artifact = await asyncio.to_thread(artifact_search, tool_name, pattern, path, glob, output_mode, runtime)
                if artifact is not None:
                    return artifact
                request = await asyncio.to_thread(search_request, tool_name, pattern, path, glob, limit, cursor, timeout_ms,
                    scan_limit, max_response_bytes, max_bytes, max_snippet_bytes)
                result = await resource_navigation.navigate(request, local_gateway_id=gateway_id)
                message = search_message(tool_name, result, output_mode, runtime, max_response_bytes)
                if time.monotonic() >= deadline:
                    raise TimeoutError
                return message
        except (TimeoutError, httpx.TimeoutException):
            return _path_error(tool_name, runtime, ResourceFileOperationError(
                "output_limit_exceeded", "文件搜索超过本次总期限", reason="deadline"))
        except (OSError, RuntimeError, ValueError, httpx.HTTPError, FederationError) as error:
            return _path_error(tool_name, runtime, error)

    replacements = {
        "ls": (sync_ls, async_ls, WorkspaceLsSchema),
        "read_file": (sync_read_file, async_read_file, WorkspaceReadFileSchema),
        "write_file": (sync_write_file, async_write_file, WorkspaceWriteFileSchema),
        "edit_file": (sync_edit_file, async_edit_file, WorkspaceEditFileSchema),
        "glob": (update_wrapper(partial(sync_search, tool_name="glob"), sync_search), update_wrapper(partial(async_search, tool_name="glob"), async_search), WorkspaceGlobSchema),
        "grep": (update_wrapper(partial(sync_search, tool_name="grep"), sync_search), update_wrapper(partial(async_search, tool_name="grep"), async_search), WorkspaceGrepSchema),
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
