"""已公布文件根的轻量导航；不登记来源、快照或上下文激活。"""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import json
import math
import mimetypes
import os
import re
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import regex
from wcmatch import glob as wcglob

from app.abstractions.resource_file_content import ResourceFileReader
from app.core.distribution_identity import load_distribution_id
from app.core.exceptions import ForbiddenError
from app.core.failures import public_failure_message
from app.core.fuzzy import is_subsequence
from app.core.path_utils import is_resource_uri_input
from app.core.resource_uri import (
    ParsedResourceUri,
    file_uri_root,
    parse_resource_uri,
    resource_display_uri,
)
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationContextDTO,
    ResourceNavigationMatchDTO,
    ResourceNavigationNodeDTO,
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
    ResourceNavigationSearchOptions,
    ResourceNavigationStatDTO,
)
from app.services.infrastructure.resource_platform.sources.resource_search_ignore import (
    IgnorePatternError,
    IgnoreScope,
)
from app.services.infrastructure.resource_platform.sources.resource_text_search import (
    ContentMatch,
    content_matches,
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
    from app.services.infrastructure.resource_platform.sources.gateway_resource_navigation import (
        GatewayResourceNavigation,
    )
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
_DEFAULT_SEARCH_OPTIONS = ResourceNavigationSearchOptions()


class FileObservation(NamedTuple):
    stat: tuple[int, ...]
    ignore: tuple[int, ...] = ()


class SearchPosition(NamedTuple):
    offset: int
    matches: int
    prefix: str


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


NAVIGATION_DEADLINE_HEADER = "X-BoxTeam-Resource-Navigation-Deadline"


def navigation_deadline(
    request: ResourceNavigationRequest, *, deadline: float | None = None,
    deadline_at: str | float | None = None,
) -> float:
    """查询限制不随路由改变；执行期限只能扣减，跨进程复用联邦 UTC 期限语义。"""
    now = time.monotonic()
    effective = min(now + request.timeout_ms / 1000, deadline) if deadline is not None else now + request.timeout_ms / 1000
    if deadline_at is not None:
        try:
            timestamp = float(deadline_at)
            if not math.isfinite(timestamp):
                raise ValueError
        except (ValueError, TypeError) as error:
            raise ResourceFileOperationError("execution_failed", "文件导航执行期限无效", reason="invalid_deadline") from error
        effective = min(effective, now + (timestamp - time.time()))
    ResourceNavigation._check_deadline(effective)
    return effective


def navigation_deadline_at(deadline: float) -> float:
    ResourceNavigation._check_deadline(deadline)
    return time.time() + (deadline - time.monotonic())


class ResourceNavigation:
    """Workspace 与 Gateway 共用一个文件访问实现，根绑定只来自显式 owner。"""

    def __init__(
        self, *, workspace_root: Path | None = None, workspace_id: str | None = None,
        project_root: Path | None = None, gateway_sources: GatewaySkillSources | None = None,
        gateway_skills_root: Path | None = None, gateway_id: str | None = None,
        file_paths: WorkspaceFilePaths | None = None,
        gateway_navigation: GatewayResourceNavigation | None = None,
        artifact_reader: ResourceFileReader | None = None,
        network_skill_reader: ResourceFileReader | None = None,
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
        self._gateway_navigation = gateway_navigation
        self._gateway_skills_root = gateway_skills_root.absolute() if gateway_skills_root is not None else None
        self._gateway_id = gateway_id
        self._artifact_reader = artifact_reader
        self._network_skill_reader = network_skill_reader

    def input_uri(self, value: str) -> str:
        """原生输入只在固定文件 owner 解释；资源 URI 留给唯一 grammar 校验。"""
        if is_resource_uri_input(value):
            return value
        if self._file_paths is None:
            raise ResourceUriResolveError("capability_denied", "此 owner 不接受原生文件路径")
        try:
            return self._file_paths.resolve(value, allow_empty=False)[1]
        except ForbiddenError as error:
            raise ResourceUriResolveError("capability_denied", "原生文件目标越出固定目录能力") from error

    def _parse_target(self, request: ResourceNavigationRequest, gateway_id: str) -> ParsedResourceUri:
        parsed = parse_resource_uri(request.uri, local_gateway_id=gateway_id)
        if self._gateway_id is not None and gateway_id != self._gateway_id:
            raise ResourceUriResolveError("scope_mismatch", "Gateway Skill 导航 owner 身份不一致")
        distribution_id = load_distribution_id() if parsed.scope == "inline" else None
        require_scope_binding(parsed.scope, parsed.scope_id, ResolutionContext(
            workspace_id=self._workspace_id, gateway_id=gateway_id,
            distribution_id=distribution_id,
        ))
        return parsed

    def _routed_request(self, request: ResourceNavigationRequest, gateway_id: str,
                        artifact_reader: ResourceFileReader | None = None) -> ResourceNavigationRequest | None:
        parsed = parse_resource_uri(request.uri, local_gateway_id=gateway_id)
        if parsed.session_artifact_kind is not None and artifact_reader is not None:
            # 模型的精确 SessionThread capability 必须在源端检查，不能经 read relay 绕过。
            return None
        if parsed.gateway_authority is None and not (
            parsed.scope == "workspace" and parsed.scope_id != self._workspace_id
        ):
            return None
        if self._gateway_navigation is None:
            raise ResourceUriResolveError("scope_mismatch", "此文件 owner 未装配目标 Gateway 路由")
        return request.model_copy(update={"uri": resource_display_uri(
            scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
            tail_segments=parsed.tail_segments, gateway_authority=parsed.gateway_authority or gateway_id,
        )})

    def navigate_sync(
        self, request: ResourceNavigationRequest, *, local_gateway_id: str, artifact_reader: ResourceFileReader | None = None,
        deadline: float | None = None,
    ) -> ResourceNavigationResultDTO:
        """同步工具复用同一解析与文件执行；HTTP 传输不借用异步观察连接。"""
        deadline = navigation_deadline(request, deadline=deadline)
        routed = self._routed_request(request, local_gateway_id, artifact_reader)
        if routed is not None:
            result = self._gateway_navigation.navigate_sync(routed, deadline=deadline)
            return self._response(result, request, deadline)
        parsed = self._parse_target(request, local_gateway_id)
        if parsed.scope == "gateway" and parsed.kind == "skills" and self._workspace_root is not None:
            if self._gateway_sources is None:
                raise ResourceUriResolveError("unknown_resource", "没有对应的 Gateway 文件 owner")
            result = self._gateway_sources.navigate_sync(
                request.model_copy(update={"uri": parsed.display_uri}), gateway_id=local_gateway_id, deadline=deadline,
            )
            return self._response(result, request, deadline)
        return self._navigate_local(parsed, request, local_gateway_id, deadline, artifact_reader)

    async def navigate(
        self, request: ResourceNavigationRequest, *, local_gateway_id: str, artifact_reader: ResourceFileReader | None = None,
        deadline: float | None = None,
    ) -> ResourceNavigationResultDTO:
        deadline = navigation_deadline(request, deadline=deadline)
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                routed = self._routed_request(request, local_gateway_id, artifact_reader)
                if routed is not None:
                    result = await self._gateway_navigation.navigate(routed, deadline=deadline)
                    return self._response(result, request, deadline)
                parsed = self._parse_target(request, local_gateway_id)
                if parsed.scope == "gateway" and parsed.kind == "skills" and self._workspace_root is not None:
                    if self._gateway_sources is None:
                        raise ResourceUriResolveError("unknown_resource", "没有对应的 Gateway 文件 owner")
                    result = await self._gateway_sources.navigate(
                        request.model_copy(update={"uri": parsed.display_uri}), gateway_id=local_gateway_id, deadline=deadline,
                    )
                    return self._response(result, request, deadline)
                # 取消等待不能中断原生阻塞 I/O；只读 worker 恢复后仍检查同一 deadline。
                return await asyncio.to_thread(self._navigate_local, parsed, request, local_gateway_id, deadline, artifact_reader)
        except TimeoutError as error:
            raise ResourceFileOperationError("output_limit_exceeded", "文件导航超过本次总期限", reason="deadline") from error

    def _navigate_local(
        self, parsed: ParsedResourceUri, request: ResourceNavigationRequest,
        gateway_id: str, deadline: float,
        artifact_reader: ResourceFileReader | None = None,
    ) -> ResourceNavigationResultDTO:
        self._check_deadline(deadline)
        excluded = self._excluded_resources(parsed, request, gateway_id)
        if parsed.kind == "skills" and parsed.tail_segments[0].startswith("http_"):
            return self._navigate_owned_file(parsed, request, gateway_id, deadline, self._network_skill_reader,
                                             owner_id=self._workspace_id if self._workspace_root is not None else self._gateway_id)
        if parsed.session_artifact_kind is not None:
            return self._navigate_owned_file(parsed, request, gateway_id, deadline, artifact_reader or self._artifact_reader, owner_id=self._workspace_id)
        pattern = self._text_pattern(request)
        root, children = self._root(parsed)
        path = self._contained(root, children, parsed)
        node = self._node(parsed, path, include_stat=request.operation == "describe")
        if request.operation not in node.operations:
            raise ResourceUriResolveError("capability_denied", "资源不允许所请求的导航操作")
        if request.operation == "describe":
            return self._response(ResourceNavigationResultDTO(node=node), request, deadline)
        if request.operation == "read":
            if parsed.kind == "attachments":
                # 原件无扩展名；MIME 与完整性仍由附件 owner 决定，正文投影复用原读取链。
                # 工作区附件按工作区权限访问；Session 私有产物才消费调用者的精确 capability。
                return self._navigate_owned_file(parsed, request, gateway_id, deadline, self._artifact_reader, owner_id=self._workspace_id)
            return self._read(path, node, request, deadline)
        binding = hashlib.sha256(json.dumps(
            {**request.model_dump(exclude={"uri", "cursor"}), "uri": parsed.display_uri, "gateway_id": gateway_id},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        resume = self._decode_cursor(request.cursor, binding)
        entries: list[ResourceNavigationNodeDTO] = []
        matches: list[ResourceNavigationMatchDTO] = []
        contexts: dict[tuple[str, int], ResourceNavigationContextDTO] = {}
        seen: set[str] = set()
        fuzzy_query = request.query.casefold() if request.search_mode == "fuzzy" and request.query is not None else ""
        # 预留固定长度的续页及截断字段，序列化后的实际正文仍在 _response 核验。
        response_bytes = self._size(ResourceNavigationResultDTO(
            node=node, truncated=True, next_cursor="x" * 512, truncation_reason="response_byte_limit",
        ))
        for child_node, hit, position in self._search_items(root, children, parsed, node, request, deadline, pattern, excluded, resume):
            match = hit.match if hit is not None else None
            relative = parse_resource_uri(child_node.uri).tail_segments[len(parsed.tail_segments):]
            name = "/".join(relative) or parsed.tail_segments[-1]
            if request.operation == "list":
                included = True
            elif request.search_mode == "content":
                included = match is not None
            elif request.search_mode == "glob":
                included = child_node.resource_shape == "file" and self._glob_matches(name, request.query)
            elif request.search_mode == "fuzzy":
                included = child_node.resource_shape == "file" and is_subsequence(fuzzy_query, name.casefold())
            else:
                included = request.query in "/".join(parse_resource_uri(child_node.uri).tail_segments[1:])
            if included and request.operation == "search" and request.search_mode != "content":
                included = self._file_included(name, request)
            if included:
                additions = {(line.uri, line.line): line for line in hit.context if (line.uri, line.line) not in contexts} if hit is not None else {}
                entry_bytes = 0 if child_node.uri in seen else len(child_node.model_dump_json().encode()) + 1
                if match is not None:
                    entry_bytes += len(match.model_dump_json().encode()) + 1
                entry_bytes += sum(len(line.model_dump_json().encode()) + 1 for line in additions.values())
                reason = "entry_limit" if (len(matches) if request.search_mode == "content" else len(entries)) == request.limit else (
                    "response_byte_limit" if response_bytes + entry_bytes > request.max_response_bytes else None
                )
                if reason is not None:
                    if not entries:
                        raise ResourceFileOperationError("output_limit_exceeded", "响应预算不足以容纳一个文件节点及续页凭据", reason="response_byte_limit")
                    # 续点指向首条未交付项；页预算不能推进越过该命中。
                    cursor = self._encode_cursor(binding, position)
                    return self._response(ResourceNavigationResultDTO(node=node, entries=tuple(entries), matches=tuple(matches), contexts=tuple(contexts.values()),
                        truncated=True, next_cursor=cursor, truncation_reason=reason), request, deadline)
                if child_node.uri not in seen:
                    entries.append(child_node)
                    seen.add(child_node.uri)
                if match is not None:
                    matches.append(match)
                contexts.update(additions)
                response_bytes += entry_bytes
        return self._response(ResourceNavigationResultDTO(node=node, entries=tuple(entries), matches=tuple(matches), contexts=tuple(contexts.values())), request, deadline)

    def _navigate_owned_file(
        self, parsed: ParsedResourceUri, request: ResourceNavigationRequest, gateway_id: str,
        deadline: float, reader: ResourceFileReader | None, *, owner_id: str | None,
    ) -> ResourceNavigationResultDTO:
        if parsed.gateway_authority is not None or parsed.scope_id != owner_id or reader is None:
            raise ResourceUriResolveError("capability_denied", "会话产物缺少此目标的原 owner 读取能力")
        pattern = self._text_pattern(request)
        node = ResourceNavigationNodeDTO.model_validate(resource_navigation_node(parsed, resource_shape="file"))
        if request.operation not in node.operations:
            raise ResourceUriResolveError("capability_denied", "会话产物只提供精确单文件能力，不开放私有目录")
        try:
            owned = reader(request.uri, request.max_bytes)
        except FileNotFoundError as error:
            raise ResourceUriResolveError("unknown_resource", "会话产物不存在或已不可用") from error
        except PermissionError as error:
            if (message := public_failure_message(error)) is None:
                raise
            raise ResourceUriResolveError("capability_denied", message) from error
        self._check_deadline(deadline)
        if request.operation == "describe":
            return self._response(ResourceNavigationResultDTO(node=node), request, deadline)
        mime_type = owned.content_type
        text_mime = mime_type.startswith("text/") or mime_type in {"application/json", "application/xml", "application/javascript"}
        media = None if text_mime or request.read_format != "auto" else mime_type.split("/", 1)[0]
        if media not in {None, "image", "audio", "video"}:
            media = "file"
        if request.operation == "read":
            return self._read_content(owned.data, node, request, deadline, media_type=media, mime_type=mime_type)
        if request.search_mode != "content":
            raise ResourceUriResolveError("capability_denied", "工具结果仅支持单文件正文搜索，不提供产物目录匹配")
        binding = hashlib.sha256(json.dumps({**request.model_dump(exclude={"uri", "cursor"}), "uri": parsed.display_uri,
                                            "gateway_id": gateway_id}, sort_keys=True).encode()).hexdigest()
        version = hashlib.sha256(owned.data).hexdigest()
        resume = self._decode_cursor(request.cursor, binding)
        offset = resume.offset
        if request.cursor is not None and (resume.prefix != version or resume.matches):
            raise ResourceFileOperationError("execution_failed", "工具结果已改变，不能使用原 cursor", reason="stale_cursor")
        if not self._file_included(parsed.tail_segments[-1], request):
            if offset:
                raise ResourceFileOperationError("execution_failed", "工具结果 cursor 超出匹配范围", reason="stale_cursor")
            return self._response(ResourceNavigationResultDTO(node=node), request, deadline)
        if len(owned.data) > request.max_bytes:
            raise ResourceFileOperationError("output_limit_exceeded", "工具结果超过搜索字节预算", reason="file_byte_limit")
        try:
            content = owned.data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ResourceFileOperationError("invalid_encoding", "工具结果不是有效 UTF-8 文本", reason="utf8") from error
        matches = []
        contexts: dict[tuple[str, int], ResourceNavigationContextDTO] = {}
        consumed = 0
        truncated = False
        reason = None
        for hit in self._content_matches(node, content, request, deadline, pattern):
            if hit is None:
                continue
            consumed += 1
            if consumed <= offset:
                continue
            additions = {(line.uri, line.line): line for line in hit.context if (line.uri, line.line) not in contexts}
            candidate = ResourceNavigationResultDTO(node=node, entries=(node,), matches=(*matches, hit.match),
                contexts=(*contexts.values(), *additions.values()), truncated=True,
                next_cursor=self._encode_cursor(binding, SearchPosition(consumed, 0, version)), truncation_reason="response_byte_limit", view="live")
            if len(matches) >= request.limit or self._size(candidate) > request.max_response_bytes:
                truncated, reason = True, "entry_limit" if len(matches) >= request.limit else "response_byte_limit"
                break
            matches.append(hit.match)
            contexts.update(additions)
        if truncated and not matches:
            raise ResourceFileOperationError("output_limit_exceeded", "预算不足以容纳一条工具结果匹配", reason="response_byte_limit")
        if consumed < offset:
            raise ResourceFileOperationError("execution_failed", "工具结果 cursor 超出匹配范围", reason="stale_cursor")
        return self._response(ResourceNavigationResultDTO(node=node, entries=(node,) if matches else (), matches=tuple(matches),
            contexts=tuple(contexts.values()),
            view="live", truncated=truncated, truncation_reason=reason,
            next_cursor=self._encode_cursor(binding, SearchPosition(offset + len(matches), 0, version)) if truncated else None), request, deadline)

    @staticmethod
    def _glob_matches(name: str, pattern: str) -> bool:
        # 保留原 glob 在各层目录匹配文件的语义；模式从不参与目标解析。
        return wcglob.globmatch(name, "**/" + pattern.lstrip("/"), flags=wcglob.GLOBSTAR | wcglob.BRACE | wcglob.DOTGLOB)

    def _file_included(self, name: str, request: ResourceNavigationRequest) -> bool:
        options = request.search_options
        return ((request.file_glob is None or self._glob_matches(name, request.file_glob))
                and (options is None or (
                    (not options.include_globs or any(self._glob_matches(name, pattern) for pattern in options.include_globs))
                    and not any(self._glob_matches(name, pattern) for pattern in options.exclude_globs))))

    @staticmethod
    def _text_pattern(request: ResourceNavigationRequest) -> regex.Pattern[str] | None:
        options = request.search_options or _DEFAULT_SEARCH_OPTIONS
        if request.operation != "search" or request.search_mode != "content":
            return None
        if request.query is None:
            raise ValueError("内容搜索缺少 query")
        query = request.query
        capitals = re.sub(r"\\.", "", query) if options.is_regex else query
        sensitive = options.case_mode == "sensitive" or (options.case_mode == "smart" and capitals.lower() != capitals)
        source = query if options.is_regex else regex.escape(query)
        if options.whole_word:
            word = r"\w" if options.word_separators is None else r"[^\s" + regex.escape(options.word_separators) + "]"
            source = f"(?<!{word})(?:{source})(?!{word})"
        try:
            return regex.compile(source, (0 if sensitive else regex.IGNORECASE) | regex.MULTILINE)
        except regex.error as error:
            raise ResourceFileOperationError("execution_failed", f"内容搜索正则无效: {error}", reason="invalid_search_pattern") from error

    @staticmethod
    def _excluded_resources(parsed: ParsedResourceUri, request: ResourceNavigationRequest, gateway_id: str) -> frozenset[str]:
        uris = request.search_options.excluded_resource_uris if request.search_options is not None else ()
        excluded: set[str] = set()
        for uri in uris:
            target = parse_resource_uri(uri, local_gateway_id=gateway_id)
            if (target.scope, target.scope_id, target.kind, target.gateway_authority) != (
                parsed.scope, parsed.scope_id, parsed.kind, parsed.gateway_authority,
            ) or target.tail_segments[:len(parsed.tail_segments)] != parsed.tail_segments:
                raise ResourceUriResolveError("scope_mismatch", "模型覆盖资源不属于本次文件查询根")
            excluded.add(target.display_uri)
        if excluded and (parsed.session_artifact_kind is not None or (parsed.kind == "skills" and parsed.tail_segments[0].startswith("http_"))):
            raise ResourceUriResolveError("capability_denied", "私有产物或网络快照不接受文件模型覆盖")
        return frozenset(excluded)

    def _search_items(
        self, root: Path, children: tuple[str, ...], parsed: ParsedResourceUri,
        node: ResourceNavigationNodeDTO, request: ResourceNavigationRequest, deadline: float, pattern: regex.Pattern[str] | None,
        excluded: frozenset[str], resume: SearchPosition,
    ) -> Iterator[tuple[ResourceNavigationNodeDTO, ContentMatch | None, SearchPosition]]:
        path = self._contained(root, children, parsed)
        candidates = self._walk(root, children, parsed, request, deadline) if node.resource_shape == "directory" else iter(((node, FileObservation(self._stat(path))),))
        root_stat = path.stat()
        prefix = hashlib.sha256(str((root_stat.st_dev, root_stat.st_ino, root_stat.st_mtime_ns)).encode())
        prefix_checked = not resume.prefix
        index = 0
        # TODO：目录前缀仍逐页观察；64 文件小页实测见 test_content_cursor_skips_completed_file_bodies。
        # 后续复用须沿原 watcher/cacheKey 生命周期，不能缓存另一份权威目录或绕过重启续页校验。
        for child, metadata in candidates:
            self._check_deadline(deadline)
            if request.search_options is not None and request.search_options.files_only and child.resource_shape != "file":
                continue
            if child.uri in excluded:
                continue
            index += 1
            # 文件观察值和 ignore 版本足以识别可变前缀；不重复序列化每条命中正文。
            previous_prefix = prefix.hexdigest()
            prefix.update(json.dumps((child.uri, metadata), separators=(",", ":")).encode())
            current_prefix = prefix.hexdigest()
            if index <= resume.offset:
                continue
            if index == resume.offset + 1 and resume.prefix:
                if (current_prefix if resume.matches else previous_prefix) != resume.prefix:
                    raise ResourceFileOperationError("execution_failed", "目录已改变，不能沿旧 cursor 续页", reason="stale_cursor")
                prefix_checked = True
            skipped = resume.matches if index == resume.offset + 1 else 0
            hits = self._file_matches(root, parsed, child, metadata, request, deadline, pattern) if (
                request.operation == "search" and request.search_mode == "content" and child.resource_shape == "file"
            ) else iter((None,))
            for match_index, hit in enumerate(hits):
                if skipped:
                    skipped -= 1
                    continue
                yield child, hit, SearchPosition(index - 1, match_index, current_prefix if match_index else previous_prefix)
            if skipped:
                raise ResourceFileOperationError("execution_failed", "文件内 cursor 的位置不存在", reason="stale_cursor")
        if not prefix_checked:
            raise ResourceFileOperationError("execution_failed", "目录已改变，旧 cursor 的位置不存在", reason="stale_cursor")

    def _file_matches(
        self, root: Path, parsed: ParsedResourceUri, child: ResourceNavigationNodeDTO, metadata: FileObservation,
        request: ResourceNavigationRequest, deadline: float, pattern: regex.Pattern[str] | None,
    ) -> Iterator[ContentMatch | None]:
        target = parse_resource_uri(child.uri)
        name = "/".join(target.tail_segments[len(parsed.tail_segments):]) or target.tail_segments[-1]
        if not self._file_included(name, request) or Path(name).suffix.lower() in _MEDIA_EXTENSIONS:
            yield None
            return
        file = self._contained(root, target.tail_segments[1:], target)
        data = self._read_bytes(file, request.max_bytes, deadline, before=metadata.stat)
        # 与既有 grep 一致跳过二进制；已知媒体不进入文本解码。
        if b"\0" in data:
            yield None
            return
        if len(data) > request.max_bytes:
            raise ResourceFileOperationError("output_limit_exceeded", "搜索文件超过读取上限，请缩小过滤或增加 max_bytes", reason="file_byte_limit")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ResourceFileOperationError("invalid_encoding", "搜索文件不是有效的 UTF-8 文本", reason="utf8") from error
        yield from self._content_matches(child, content, request, deadline, pattern)

    def _content_matches(
        self, child: ResourceNavigationNodeDTO, content: str, request: ResourceNavigationRequest,
        deadline: float, pattern: regex.Pattern[str] | None,
    ) -> Iterator[ContentMatch | None]:
        if pattern is None:
            raise ValueError("内容搜索缺少已编译查询")
        found = False
        try:
            for hit in content_matches(child.uri, content, request, deadline, pattern, time.monotonic):
                found = True
                yield hit
        except TimeoutError as error:
            raise ResourceFileOperationError("output_limit_exceeded", "文件内容匹配超过本次总期限", reason="deadline") from error
        if not found:
            yield None

    @staticmethod
    def _stat(path: Path) -> tuple[int, ...]:
        metadata = path.stat()
        return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns

    def _read(
        self, path: Path, node: ResourceNavigationNodeDTO,
        request: ResourceNavigationRequest, deadline: float,
    ) -> ResourceNavigationResultDTO:
        # 字节预算覆盖跳过的行；不为读取登记身份、快照或 activation。
        data = self._read_bytes(path, request.max_bytes, deadline)
        filename = parse_resource_uri(node.uri).tail_segments[-1]
        media_type = _MEDIA_EXTENSIONS.get(Path(filename).suffix.lower()) if request.read_format == "auto" else None
        mime_type = mimetypes.guess_type("file" + Path(filename).suffix)[0] or "application/octet-stream"
        return self._read_content(data, node, request, deadline, media_type=media_type, mime_type=mime_type)

    def _read_bytes(self, path: Path, maximum: int, deadline: float, *, before: tuple[int, ...] | None = None) -> bytes:
        observed = before if before is not None else self._stat(path)
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        self._check_deadline(deadline)
        if self._stat(path) != observed:
            raise ResourceFileOperationError("execution_failed", "文件在读取期间改变，请重新读取", reason="file_changed")
        return data

    def _read_content(
        self, data: bytes, node: ResourceNavigationNodeDTO, request: ResourceNavigationRequest, deadline: float,
        *, media_type: str | None, mime_type: str,
    ) -> ResourceNavigationResultDTO:
        self._check_deadline(deadline)
        truncated = len(data) > request.max_bytes
        content_hash = None if truncated else "sha256:" + hashlib.sha256(data).hexdigest()
        if media_type is not None:
            if truncated:
                raise ResourceFileOperationError("output_limit_exceeded", "媒体原件超过读取字节上限，请显式增加 max_bytes", reason="file_byte_limit")
            result = ResourceNavigationResultDTO(
                node=node, content=base64.b64encode(data).decode("ascii"), content_encoding="base64",
                media_type=media_type, mime_type=mime_type,
                content_hash=content_hash,
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
            content_hash=content_hash,
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
    ) -> Iterator[tuple[ResourceNavigationNodeDTO, FileObservation]]:
        pending: list[tuple[tuple[str, ...], IgnoreScope | None]] = [(children, None)]
        visited: set[Path] = set()
        scanned = 0
        remaining_ignore_bytes = request.max_bytes

        def read_ignore(parts: tuple[str, ...]) -> tuple[str, tuple[int, ...]] | None:
            nonlocal remaining_ignore_bytes
            candidate = root.joinpath(*parts)
            if not candidate.exists() and not candidate.is_symlink():
                return None
            self._check_deadline(deadline)
            file = self._contained(root, parts, parsed)
            before = self._stat(file)
            if not stat.S_ISREG(before[2]):
                raise ResourceFileOperationError("execution_failed", "ignore 规则不是普通文件", reason="invalid_ignore_file")
            data = self._read_bytes(file, remaining_ignore_bytes, deadline, before=before)
            if len(data) > remaining_ignore_bytes:
                raise ResourceFileOperationError("output_limit_exceeded", "ignore 规则累计读取超过 max_bytes", reason="ignore_byte_limit")
            remaining_ignore_bytes -= len(data)
            try:
                return data.decode("utf-8-sig"), before
            except UnicodeDecodeError as error:
                raise ResourceFileOperationError("invalid_encoding", "ignore 规则不是有效 UTF-8", reason="ignore_utf8") from error

        while pending:
            self._check_deadline(deadline)
            relative, scope = pending.pop()
            directory = self._contained(root, relative, parsed)
            if directory in visited:
                continue
            visited.add(directory)
            if request.operation == "search" and request.search_options is not None and request.search_options.ignore_mode == "respect":
                try:
                    scope = IgnoreScope.load(relative, scope, read_ignore)
                except IgnorePatternError as error:
                    raise ResourceFileOperationError("execution_failed", str(error), reason="invalid_ignore_pattern") from error
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
                if parsed.kind in {"files", "attachments"} and self._file_paths.is_private(child, root_uri=parsed.display_uri):
                    continue
                if request.search_mode == "content" and child.name in {".git", "node_modules", ".boxteam"} and (
                    request.search_options is None or request.search_options.ignore_mode == "default"
                ):
                    continue
                name = "/".join(child_parts[len(children):])
                if request.search_options is not None and any(
                    self._glob_matches(name, pattern) or self._glob_matches(name + "/", pattern)
                    for pattern in request.search_options.exclude_globs
                ):
                    continue
                if scope is not None and scope.excludes(child_parts, child.is_dir()):
                    continue
                child_path = self._contained(root, child_parts, parsed)
                uri = resource_display_uri(
                    scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
                    tail_segments=(parsed.tail_segments[0], *child_parts),
                )
                child_node = self._node(parse_resource_uri(uri), child_path)
                # 仅校验续页前缀的可变视图，不作为文件内容 hash 或历史 revision。
                yield child_node, FileObservation(self._stat(child_path), scope.version if scope is not None else ())
                if request.operation == "search" and child_node.resource_shape == "directory":
                    pending.append((child_parts, scope))
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
    def _encode_cursor(binding: str, position: SearchPosition) -> str:
        raw = json.dumps({"v": 2, "binding": binding, "offset": position.offset,
                          "matches": position.matches, "prefix": position.prefix}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None, binding: str) -> SearchPosition:
        if cursor is None:
            return SearchPosition(0, 0, "")
        try:
            value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        except (ValueError, UnicodeError) as error:
            raise ResourceFileOperationError("execution_failed", "文件导航 cursor 无效", reason="invalid_cursor") from error
        if (not isinstance(value, dict) or set(value) != {"v", "binding", "offset", "matches", "prefix"}
                or value["v"] != 2 or value["binding"] != binding
                or type(value["offset"]) is not int or value["offset"] < 0
                or type(value["matches"]) is not int or value["matches"] < 0
                or value["offset"] + value["matches"] == 0
                or not isinstance(value["prefix"], str) or len(value["prefix"]) != 64):
            raise ResourceFileOperationError("execution_failed", "cursor 与目标、操作或查询限制不一致", reason="invalid_cursor")
        return SearchPosition(value["offset"], value["matches"], value["prefix"])

    def _root(self, parsed: ParsedResourceUri) -> tuple[Path, tuple[str, ...]]:
        if parsed.kind in {"files", "attachments"} and self._file_paths is not None:
            root_uri = resource_display_uri(scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
                                            tail_segments=parsed.tail_segments[:1])
            try:
                root, _uri = self._file_paths.resolve(root_uri)
            except ForbiddenError as error:
                raise ResourceUriResolveError("capability_denied", "私有存储只允许由业务 owner 访问") from error
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

    def _contained(self, root: Path, children: tuple[str, ...], parsed: ParsedResourceUri) -> Path:
        resolved_root = _resolve_path(root)
        if resolved_root != root:
            raise ResourceUriResolveError("capability_denied", "固定资源根已被符号链接替换")
        path = _resolve_path(resolved_root.joinpath(*children))
        if not path.is_relative_to(resolved_root):
            raise ResourceUriResolveError("capability_denied", "文件节点越出已公布根")
        if parsed.kind in {"files", "attachments"}:
            try:
                self._file_paths.require_access(resolved_root.joinpath(*children), root_uri=file_uri_root(parsed.display_uri))
            except ForbiddenError as error:
                raise ResourceUriResolveError("capability_denied", "私有存储只允许由业务 owner 访问") from error
        for depth in range(1, len(children)):
            ancestor = resolved_root.joinpath(*children[:depth])
            if ancestor.exists() and not ancestor.is_dir():
                raise ResourceUriResolveError("capability_denied", "单文件节点不允许追加子路径")
        if not path.exists():
            raise ResourceUriResolveError("unknown_resource", "资源不存在")
        return path

    @staticmethod
    def _node(parsed: ParsedResourceUri, path: Path, *, include_stat: bool = False) -> ResourceNavigationNodeDTO:
        metadata = path.stat()
        mode = metadata.st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ResourceUriResolveError("capability_denied", "资源只公布普通文件与目录")
        node = resource_navigation_node(parsed, resource_shape="directory" if stat.S_ISDIR(mode) else "file")
        result = ResourceNavigationNodeDTO.model_validate(node)
        if include_stat:
            return result.model_copy(update={"stat": ResourceNavigationStatDTO(size=metadata.st_size,
                modified_at_ms=metadata.st_mtime_ns / 1_000_000,
                changed_at_ms=metadata.st_ctime_ns / 1_000_000)})
        return result
