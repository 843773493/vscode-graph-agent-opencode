"""已公布文件根的轻量导航；不登记来源、快照或上下文激活。"""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import json
import mimetypes
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.distribution_identity import load_distribution_id
from app.core.exceptions import ForbiddenError
from app.core.resource_uri import (
    ParsedResourceUri,
    parse_resource_uri,
    resource_display_uri,
)
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationNodeDTO,
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
    require_scope_binding,
    resource_navigation_node,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResolutionContext,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

if TYPE_CHECKING:
    from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
        GatewaySkillSources,
    )


# 保留既有 read_file 媒体集合；未知扩展名仍是严格文本，不猜测非 UTF-8 数据。
_MEDIA_EXTENSIONS = {
    **dict.fromkeys((".png", ".jpeg", ".jpg", ".webp", ".gif", ".heic", ".heif"), "image"),
    **dict.fromkeys((".mp4", ".mpeg", ".mov", ".avi", ".flv", ".mpg", ".webm", ".wmv", ".3gpp"), "video"),
    **dict.fromkeys((".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"), "audio"),
    **dict.fromkeys((".pdf", ".ppt", ".pptx"), "file"),
}


def _resolve_path(path: Path) -> Path:
    try:
        return path.resolve()
    except RuntimeError as error:
        raise ResourceUriResolveError("capability_denied", "资源路径包含符号链接循环") from error


class ResourceFileOperationError(ValueError):
    """文件执行失败与 URI grammar/归属解析失败保持独立错误域。"""

    def __init__(self, code: str, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.code, self.reason = code, reason

    def detail(self) -> dict[str, str]:
        return {"owner": "resource_file_operations", "code": self.code,
                "reason": self.reason, "message": str(self)}


class ResourceNavigation:
    """Workspace 与 Gateway 共用一个文件访问实现，根绑定只来自显式 owner。"""

    def __init__(
        self, *, workspace_root: Path | None = None, workspace_id: str | None = None,
        project_root: Path | None = None, gateway_sources: GatewaySkillSources | None = None,
        gateway_skills_root: Path | None = None, gateway_id: str | None = None,
        file_paths: WorkspaceFilePaths | None = None,
    ) -> None:
        if (workspace_root is None) == (gateway_skills_root is None):
            raise ValueError("导航 owner 必须且只能绑定 Workspace 或 Gateway Skill 域")
        if workspace_root is not None and not workspace_id:
            raise ValueError("Workspace 导航必须显式绑定 workspace_id")
        if gateway_skills_root is not None and not gateway_id:
            raise ValueError("Gateway Skill 导航必须显式绑定 gateway_id")
        self._workspace_root = _resolve_path(workspace_root) if workspace_root is not None else None
        self._workspace_id = workspace_id
        if workspace_root is not None and file_paths is None:
            raise ValueError("Workspace 导航必须复用已装配的文件路径 owner")
        self._file_paths = file_paths
        self._project_root = _resolve_path(project_root or Path.cwd())
        self._gateway_sources = gateway_sources
        self._gateway_skills_root = gateway_skills_root.absolute() if gateway_skills_root is not None else None
        self._gateway_id = gateway_id

    def input_uri(self, value: str) -> str:
        """原生输入只在固定文件 owner 解释；资源 URI 留给唯一 grammar 校验。"""
        if "://" in value:
            return value
        if self._file_paths is None:
            raise ResourceUriResolveError("capability_denied", "此 owner 不接受原生文件路径")
        try:
            return self._file_paths.resolve(value, allow_empty=False)[1]
        except ForbiddenError as error:
            raise ResourceUriResolveError("capability_denied", "原生文件目标越出固定目录能力") from error

    def _parse_target(self, request: ResourceNavigationRequest, gateway_id: str) -> ParsedResourceUri:
        parsed = parse_resource_uri(request.uri, local_gateway_id=gateway_id)
        if parsed.gateway_authority is not None:
            raise ResourceUriResolveError("scope_mismatch", "此入口只解析固定本机 Gateway 的资源")
        if self._gateway_id is not None and gateway_id != self._gateway_id:
            raise ResourceUriResolveError("scope_mismatch", "Gateway Skill 导航 owner 身份不一致")
        distribution_id = load_distribution_id() if parsed.scope == "inline" else None
        require_scope_binding(parsed.scope, parsed.scope_id, ResolutionContext(
            workspace_id=self._workspace_id, gateway_id=gateway_id,
            distribution_id=distribution_id,
        ))
        return parsed

    def navigate_sync(
        self, request: ResourceNavigationRequest, *, local_gateway_id: str,
    ) -> ResourceNavigationResultDTO:
        """同步工具复用同一解析与文件执行；HTTP 传输不借用异步观察连接。"""
        deadline = time.monotonic() + request.timeout_ms / 1000
        parsed = self._parse_target(request, local_gateway_id)
        if parsed.scope == "gateway" and parsed.kind == "skills" and self._workspace_root is not None:
            if self._gateway_sources is None:
                raise ResourceUriResolveError("unknown_resource", "没有对应的 Gateway 文件 owner")
            result = self._gateway_sources.navigate_sync(
                request.model_copy(update={"uri": parsed.display_uri}), gateway_id=local_gateway_id,
            )
            return self._response(result, request, deadline)
        return self._navigate_local(parsed, request, local_gateway_id, deadline)

    async def navigate(
        self, request: ResourceNavigationRequest, *, local_gateway_id: str,
    ) -> ResourceNavigationResultDTO:
        deadline = time.monotonic() + request.timeout_ms / 1000
        parsed = self._parse_target(request, local_gateway_id)
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                if parsed.scope == "gateway" and parsed.kind == "skills" and self._workspace_root is not None:
                    if self._gateway_sources is None:
                        raise ResourceUriResolveError("unknown_resource", "没有对应的 Gateway 文件 owner")
                    result = await self._gateway_sources.navigate(
                        request.model_copy(update={"uri": parsed.display_uri}), gateway_id=local_gateway_id,
                    )
                    return self._response(result, request, deadline)
                # 取消等待不能中断原生阻塞 I/O；只读 worker 恢复后仍检查同一 deadline。
                return await asyncio.to_thread(self._navigate_local, parsed, request, local_gateway_id, deadline)
        except TimeoutError as error:
            raise ResourceFileOperationError("output_limit_exceeded", "文件导航超过本次总期限", reason="deadline") from error

    def _navigate_local(
        self, parsed: ParsedResourceUri, request: ResourceNavigationRequest,
        gateway_id: str, deadline: float,
    ) -> ResourceNavigationResultDTO:
        self._check_deadline(deadline)
        root, children = self._root(parsed)
        path = self._contained(root, children, parsed)
        node = self._node(parsed, path)
        if request.operation not in node.operations:
            raise ResourceUriResolveError("capability_denied", "资源不允许所请求的导航操作")
        if request.operation == "describe":
            return self._response(ResourceNavigationResultDTO(node=node), request, deadline)
        if request.operation == "read":
            return self._read(path, node, request, deadline)
        binding = hashlib.sha256(json.dumps(
            {**request.model_dump(exclude={"uri", "cursor"}), "uri": parsed.display_uri, "gateway_id": gateway_id},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        # TODO：多页目前重复有界扫描，待主体完成后按实测优化，不能引入第二目录事实源。
        offset, expected_prefix = self._decode_cursor(request.cursor, binding)
        root_stat = path.stat()
        prefix = hashlib.sha256(str((root_stat.st_dev, root_stat.st_ino, root_stat.st_mtime_ns)).encode())
        prefix_checked = offset == 0
        consumed, consumed_prefix = 0, prefix.hexdigest()
        entries: list[ResourceNavigationNodeDTO] = []
        # 预留固定长度的续页及截断字段，序列化后的实际正文仍在 _response 核验。
        response_bytes = self._size(ResourceNavigationResultDTO(
            node=node, truncated=True, next_cursor="x" * 512, truncation_reason="response_byte_limit",
        ))
        for index, (child_node, metadata) in enumerate(self._walk(root, children, parsed, request, deadline), start=1):
            prefix.update(json.dumps((child_node.uri, metadata), separators=(",", ":")).encode())
            if index <= offset:
                if index == offset:
                    prefix_checked = prefix.hexdigest() == expected_prefix
                    if not prefix_checked:
                        raise ResourceFileOperationError("execution_failed", "目录已改变，不能沿旧 cursor 续页", reason="stale_cursor")
                    consumed, consumed_prefix = index, prefix.hexdigest()
                continue
            matches = request.operation == "list" or request.query in "/".join(parse_resource_uri(child_node.uri).tail_segments[1:])
            if matches:
                entry_bytes = len(child_node.model_dump_json().encode()) + 1
                reason = "entry_limit" if len(entries) == request.limit else (
                    "response_byte_limit" if response_bytes + entry_bytes > request.max_response_bytes else None
                )
                if reason is not None:
                    if not entries:
                        raise ResourceFileOperationError("output_limit_exceeded", "响应预算不足以容纳一个文件节点及续页凭据", reason="response_byte_limit")
                    cursor = self._encode_cursor(binding, consumed, consumed_prefix)
                    return self._response(ResourceNavigationResultDTO(node=node, entries=tuple(entries),
                        truncated=True, next_cursor=cursor, truncation_reason=reason), request, deadline)
                entries.append(child_node)
                response_bytes += entry_bytes
            consumed, consumed_prefix = index, prefix.hexdigest()
        if not prefix_checked:
            raise ResourceFileOperationError("execution_failed", "目录已改变，旧 cursor 的位置不存在", reason="stale_cursor")
        return self._response(ResourceNavigationResultDTO(node=node, entries=tuple(entries)), request, deadline)

    def _read(
        self, path: Path, node: ResourceNavigationNodeDTO,
        request: ResourceNavigationRequest, deadline: float,
    ) -> ResourceNavigationResultDTO:
        # 字节预算覆盖跳过的行；不为读取登记身份、快照或 activation。
        with path.open("rb") as stream:
            data = stream.read(request.max_bytes + 1)
        self._check_deadline(deadline)
        truncated = len(data) > request.max_bytes
        filename = parse_resource_uri(node.uri).tail_segments[-1]
        media_type = _MEDIA_EXTENSIONS.get(Path(filename).suffix.lower()) if request.read_format == "auto" else None
        if media_type is not None:
            if truncated:
                raise ResourceFileOperationError("output_limit_exceeded", "媒体原件超过读取字节上限，请显式增加 max_bytes", reason="file_byte_limit")
            result = ResourceNavigationResultDTO(
                node=node, content=base64.b64encode(data).decode("ascii"), content_encoding="base64",
                media_type=media_type, mime_type=mimetypes.guess_type("file" + Path(filename).suffix)[0] or "application/octet-stream",
            )
            # 媒体必须完整；不能裁剪 base64 后伪报有效原件。
            return self._response(result, request, deadline)
        try:
            content = codecs.getincrementaldecoder("utf-8")().decode(data[:request.max_bytes], final=not truncated)
        except UnicodeDecodeError as error:
            raise ResourceFileOperationError("invalid_encoding", "文件内容不是有效的 UTF-8 文本", reason="utf8") from error
        reason = "file_byte_limit" if truncated else None
        if request.line_offset is not None or request.max_lines is not None:
            lines = content.splitlines(keepends=True)
            start = (request.line_offset or 1) - 1
            if start >= len(lines) and (start > 0 or content):
                if truncated:
                    raise ResourceFileOperationError("output_limit_exceeded", "行起点超过本次读取字节窗口，请显式增加 max_bytes", reason="file_byte_limit")
                raise ResourceFileOperationError("execution_failed", "行起点超过文件行数", reason="invalid_range")
            end = start + request.max_lines if request.max_lines is not None else len(lines)
            if end < len(lines):
                truncated, reason = True, "line_limit"
            content = "".join(lines[start:end])
        result = ResourceNavigationResultDTO(
            node=node, content=content, content_encoding="utf-8", media_type="text",
            line_offset=request.line_offset, truncated=truncated, truncation_reason=reason,
        )
        if self._size(result) > request.max_response_bytes:
            # JSON 转义计入总响应，只裁完整字符并披露截断。
            low, high = 0, len(content)
            while low < high:
                self._check_deadline(deadline)
                middle = (low + high + 1) // 2
                candidate = result.model_copy(update={"content": content[:middle], "truncated": True,
                                                     "truncation_reason": "response_byte_limit"})
                if self._size(candidate) <= request.max_response_bytes:
                    low = middle
                else:
                    high = middle - 1
            result = result.model_copy(update={"content": content[:low], "truncated": True,
                                              "truncation_reason": "response_byte_limit"})
        return self._response(result, request, deadline)

    def _walk(
        self, root: Path, children: tuple[str, ...], parsed: ParsedResourceUri,
        request: ResourceNavigationRequest, deadline: float,
    ) -> Iterator[tuple[ResourceNavigationNodeDTO, tuple[int, ...]]]:
        pending = [children]
        visited: set[Path] = set()
        scanned = 0
        while pending:
            self._check_deadline(deadline)
            relative = pending.pop()
            directory = self._contained(root, relative, parsed)
            if directory in visited:
                continue
            visited.add(directory)
            before = directory.stat().st_mtime_ns
            bounded_children: list[Path] = []
            # Path.iterdir 在部分 Python 版本会先调用 listdir；scandir 才真正逐项消费。
            with os.scandir(directory) as children_stream:
                for child in children_stream:
                    self._check_deadline(deadline)
                    scanned += 1
                    if scanned > request.scan_limit:
                        raise ResourceFileOperationError("output_limit_exceeded", "目录扫描达到本次节点上限，请缩小目标范围", reason="scan_limit")
                    bounded_children.append(directory / child.name)
            if directory.stat().st_mtime_ns != before:
                raise ResourceFileOperationError("execution_failed", "目录在枚举期间改变，请重新读取", reason="directory_changed")
            for child in sorted(bounded_children, key=lambda item: item.name):
                self._check_deadline(deadline)
                child_parts = (*relative, child.name)
                # 私有存储不作为文件树 child 发布；直接请求和别名仍在 _contained 拒绝。
                if parsed.kind == "files" and child_parts[0] == ".boxteam":
                    continue
                child_path = self._contained(root, child_parts, parsed)
                uri = resource_display_uri(
                    scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
                    tail_segments=(parsed.tail_segments[0], *child_parts),
                )
                child_node = self._node(parse_resource_uri(uri), child_path)
                metadata = child_path.stat()
                # 仅校验续页前缀的可变视图，不作为文件内容 hash 或历史 revision。
                yield child_node, (metadata.st_dev, metadata.st_ino, metadata.st_mode,
                                   metadata.st_size, metadata.st_mtime_ns)
                if request.operation == "search" and child_node.resource_shape == "directory":
                    pending.append(child_parts)
            if request.operation == "list":
                break

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise ResourceFileOperationError("output_limit_exceeded", "文件导航超过本次总期限", reason="deadline")

    @staticmethod
    def _size(result: ResourceNavigationResultDTO) -> int:
        return len(result.model_dump_json().encode("utf-8"))

    def _response(self, result: ResourceNavigationResultDTO, request: ResourceNavigationRequest,
                  deadline: float) -> ResourceNavigationResultDTO:
        self._check_deadline(deadline)
        if self._size(result) > request.max_response_bytes:
            raise ResourceFileOperationError("output_limit_exceeded", "资源导航响应超过总字节上限", reason="response_byte_limit")
        self._check_deadline(deadline)
        return result

    @staticmethod
    def _encode_cursor(binding: str, offset: int, prefix: str) -> str:
        raw = json.dumps({"v": 1, "binding": binding, "offset": offset, "prefix": prefix}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None, binding: str) -> tuple[int, str | None]:
        if cursor is None:
            return 0, None
        try:
            value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        except (ValueError, UnicodeError) as error:
            raise ResourceFileOperationError("execution_failed", "文件导航 cursor 无效", reason="invalid_cursor") from error
        if (not isinstance(value, dict) or set(value) != {"v", "binding", "offset", "prefix"}
                or value["v"] != 1 or value["binding"] != binding
                or type(value["offset"]) is not int or value["offset"] <= 0
                or not isinstance(value["prefix"], str) or len(value["prefix"]) != 64):
            raise ResourceFileOperationError("execution_failed", "cursor 与目标、操作或查询限制不一致", reason="invalid_cursor")
        return value["offset"], value["prefix"]

    def _root(self, parsed: ParsedResourceUri) -> tuple[Path, tuple[str, ...]]:
        if parsed.kind == "files" and self._file_paths is not None:
            root_uri = resource_display_uri(scope=parsed.scope, scope_id=parsed.scope_id, kind="files",
                                            tail_segments=parsed.tail_segments[:1])
            root, _uri = self._file_paths.resolve(root_uri)
            return root, parsed.tail_segments[1:]
        if parsed.kind == "agent-spec" and parsed.scope == "workspace" and self._workspace_root is not None:
            return self._workspace_root, ("AGENTS.md",)
        if parsed.kind != "skills":
            raise ResourceUriResolveError("capability_denied", "该资源由领域 owner 访问，不公开文件树")
        if parsed.scope == "workspace" and self._workspace_root is not None:
            collection = self._workspace_root / ".boxteam" / "skills"
        elif parsed.scope == "inline" and self._workspace_root is not None:
            collection = self._project_root / "resources" / "skills"
        elif parsed.scope == "gateway" and self._gateway_skills_root is not None:
            collection = self._gateway_skills_root
        else:
            raise ResourceUriResolveError("unknown_resource", "此 owner 未公布对应的 Skill 根")
        boundary = self._workspace_root if parsed.scope == "workspace" else (
            self._project_root if parsed.scope == "inline" else collection
        )
        if collection.is_symlink() or not _resolve_path(collection).is_relative_to(boundary):
            raise ResourceUriResolveError("capability_denied", "Skill 固定根越出 owner 边界")
        root = collection / parsed.tail_segments[0]
        if root.is_symlink() or not _resolve_path(root).is_relative_to(_resolve_path(collection)):
            raise ResourceUriResolveError("capability_denied", "Skill 包根越出固定根")
        if not root.is_dir():
            raise ResourceUriResolveError("unknown_resource", "Skill 包不存在")
        return _resolve_path(root), parsed.tail_segments[1:]

    @staticmethod
    def _contained(root: Path, children: tuple[str, ...], parsed: ParsedResourceUri) -> Path:
        resolved_root = _resolve_path(root)
        if resolved_root != root:
            raise ResourceUriResolveError("capability_denied", "固定资源根已被符号链接替换")
        path = _resolve_path(resolved_root.joinpath(*children))
        if not path.is_relative_to(resolved_root):
            raise ResourceUriResolveError("capability_denied", "文件节点越出已公布根")
        if parsed.kind == "files":
            relative = path.relative_to(resolved_root).parts
            if (children and children[0] == ".boxteam") or (relative and relative[0] == ".boxteam") or path.is_relative_to(_resolve_path(resolved_root / ".boxteam")):
                raise ResourceUriResolveError("capability_denied", "工作区私有存储只允许由业务 owner 访问")
        for depth in range(1, len(children)):
            ancestor = resolved_root.joinpath(*children[:depth])
            if ancestor.exists() and not ancestor.is_dir():
                raise ResourceUriResolveError("capability_denied", "单文件节点不允许追加子路径")
        if not path.exists():
            raise ResourceUriResolveError("unknown_resource", "资源不存在")
        return path

    @staticmethod
    def _node(parsed: ParsedResourceUri, path: Path) -> ResourceNavigationNodeDTO:
        mode = path.stat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ResourceUriResolveError("capability_denied", "资源只公布普通文件与目录")
        node = resource_navigation_node(parsed, resource_shape="directory" if stat.S_ISDIR(mode) else "file")
        return ResourceNavigationNodeDTO.model_validate(node)
