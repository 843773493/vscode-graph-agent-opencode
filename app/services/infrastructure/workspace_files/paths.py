"""文件输入在目标机器归一；持久引用只使用统一资源 URI。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from pathlib import Path
from typing import Protocol, TypeVar

import httpx
from pydantic import ValidationError

from app.core import resource_uri
from app.core.atomic_fs import native_io_path
from app.core.exceptions import ForbiddenError
from app.core.failures import failure_detail
from app.core.path_utils import (
    is_resource_uri_input,
    resolve_native_file_path,
    safe_join,
)
from app.core.resource_uri import (
    ParsedResourceUri,
    parse_resource_uri,
    resource_display_uri,
)
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.schemas.gateway.native_directories import (
    NativeDirectoryBindingDTO,
    NativeDirectoryResolveDTO,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.workspace_files.filesystem import file_access

_NativeReply = TypeVar("_NativeReply", NativeDirectoryBindingDTO, NativeDirectoryResolveDTO)


class AttachmentDirectoryPaths(Protocol):
    def resolve_directory_uri(self, parsed: ParsedResourceUri, *, for_write: bool = False, materialize: bool = True,
                              require_directory: bool = False) -> Path: ...

    def access_files(self, *attachment_ids: str, for_write: bool = False) -> AbstractContextManager[None]: ...

    async def settle_working_directory(self, reference: str) -> None: ...

    def admit_directory(self, attachment_id: str) -> AbstractAsyncContextManager[None]: ...


class NativeDirectoryBindings(Protocol):
    def bind(self, root: Path) -> str: ...
    def resolve(self, parsed: ParsedResourceUri) -> Path: ...


class GatewayNativeDirectoryClient:
    """仅经同机认证控制链临时取根；重启后不依赖请求缓存。"""

    def __init__(self, *, workspace_id: str, connection: GatewayTransportConnection,
                 local_token_supplier: Callable[[], str]) -> None:
        self._workspace_id = workspace_id
        self._connection = connection
        self._token = local_token_supplier

    def _request(self, method: str, suffix: str, *, response_type: type[APIResponse[_NativeReply]],
                 body: dict[str, str] | None = None) -> _NativeReply:
        gateway = httpx.URL(self._connection.resolve_gateway_url())
        if (gateway.scheme not in {"http", "https"}
                or gateway.host not in {"127.0.0.1", "::1", "localhost"}
                or gateway.userinfo or gateway.path != "/" or gateway.query or gateway.fragment):
            raise ValueError("原生目录控制链 Gateway URL 必须是无凭据、路径和查询的同机 loopback 地址")
        request_id = get_current_request_id()
        headers = {"X-Local-Token": self._token()}
        if request_id is not None:
            headers["X-Request-ID"] = request_id
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            response = client.request(
                method, gateway.copy_with(path=f"/api/gateway/native-directories/workspaces/{self._workspace_id}" + suffix),
                json=body, headers=headers, timeout=self._connection.resolve_timeout_seconds(),
            )
        response.raise_for_status()
        received = response.headers.get("X-Request-ID")
        if not received or (request_id is not None and received != request_id):
            raise ValueError("原生根绑定控制链 request_id 不一致")
        operation = "bind" if method == "POST" else "resolve"
        try:
            result = response_type.model_validate_json(response.content)
        except ValidationError as error:
            rules = sorted({item["type"] for item in error.errors(
                include_url=False, include_context=False, include_input=False,
            )})
            detail = (f"原生目录控制链回包校验失败: operation={operation}, "
                      f"error_count={error.error_count()}, rules={','.join(rules)}")
            error.add_note(f"原始响应={response.content!r}")
            raise ValueError(failure_detail(error, stage="response", label=detail)) from error
        if result.code != 0 or result.data is None or result.request_id != received:
            invalid = ValueError("原生目录控制链回执无效")
            invalid.add_note(f"原始响应={response.content!r}")
            raise ValueError(failure_detail(invalid, stage="envelope",
                             label=f"原生目录控制链回执无效: operation={operation}")) from invalid
        return result.data

    def bind(self, root: Path) -> str:
        result = self._request("POST", "", response_type=APIResponse[NativeDirectoryBindingDTO],
                               body={"native_root": str(root)})
        return result.uri

    def resolve(self, parsed: ParsedResourceUri) -> Path:
        root_uri = resource_uri.file_uri_root(parsed.display_uri)
        result = self._request("GET", f"/{parsed.scope_id}/{parsed.tail_segments[0]}",
                               response_type=APIResponse[NativeDirectoryResolveDTO])
        if result.uri != root_uri:
            raise ValueError("原生根解析控制链 owner 或 request_id 不一致")
        root = Path(result.native_root)
        if not root.is_absolute():
            raise ValueError("原生根解析控制链返回了相对路径")
        return root


class WorkspaceFilePaths:
    def __init__(self, *, workspace: Path, workspace_id: str, bindings: NativeDirectoryBindings | None,
                 attachments: AttachmentDirectoryPaths | None = None) -> None:
        self.workspace, self.workspace_id, self.bindings = workspace, workspace_id, bindings
        self.attachments = attachments
        self.workspace_uri = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root",))

    @contextmanager
    def access(self, *values: str, for_write: bool = False) -> Iterator[None]:
        """原文件操作持有附件原 holder 至正文 I/O 结束，不产生永久关联。"""
        references = []
        for value in values:
            if is_resource_uri_input(value):
                parsed = parse_resource_uri(value, local_gateway_id=get_current_gateway_id())
                if parsed.kind == "attachments":
                    references.append(parsed)
        if not references:
            yield
            return
        if self.attachments is None:
            raise RuntimeError("文件 owner 尚未装配附件目录解析")
        with self.attachments.access_files(*(value.tail_segments[0] for value in references), for_write=for_write):
            for value in references:
                self.attachments.resolve_directory_uri(value)
            yield

    async def settle_accesses(self, *values: str) -> None:
        if self.attachments is not None:
            roots = {resource_uri.file_uri_root(value) for value in values
                     if is_resource_uri_input(value) and parse_resource_uri(value).kind == "attachments"}
            for root in sorted(roots):
                await self.attachments.settle_working_directory(root)

    @asynccontextmanager
    async def admit(self, value: str) -> AsyncIterator[None]:
        parsed = parse_resource_uri(value, local_gateway_id=get_current_gateway_id())
        if parsed.kind != "attachments":
            yield
            return
        if self.attachments is None:
            raise RuntimeError("文件 owner 尚未装配附件目录解析")
        if parsed.scope_id != self.workspace_id or parsed.gateway_authority is not None:
            raise ValueError("附件目录 URI 不属于固定目标工作区")
        async with self.attachments.admit_directory(parsed.tail_segments[0]):
            yield

    def _bindings(self) -> NativeDirectoryBindings:
        if self.bindings is None:
            raise RuntimeError("目标文件 owner 尚未装配原生 Gateway 目录根绑定")
        return self.bindings

    def canonical_uri(self, parsed: ParsedResourceUri, root: Path) -> str:
        """目标 owner 只归一现存逻辑路径段；未创建后缀保留请求拼写。"""
        if os.name != "nt" or parsed.kind != "files":
            return parsed.display_uri
        from app.runtime.windows_fs import entry_name

        parts: list[str] = []
        current = root
        for index, name in enumerate(parsed.tail_segments[1:]):
            current = current / name
            try:
                actual = entry_name(native_io_path(current))
            except FileNotFoundError:
                # 新建操作沿原父目录 holder 发布后才产生文件身份，不能猜测未存在的段。
                parts.extend(parsed.tail_segments[index + 1:])
                break
            parts.append(actual)
            current = current.with_name(actual)
        return resource_display_uri(scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
                                    tail_segments=(parsed.tail_segments[0], *parts),
                                    gateway_authority=parsed.gateway_authority)

    def _private_path_filter(self, *, root_uri: str | None = None) -> tuple[Path, Callable[[Path], bool]]:
        if root_uri is not None and parse_resource_uri(root_uri).kind == "attachments":
            root, _ = self.resolve(resource_uri.file_uri_root(root_uri), materialize=False)
            attachment_scope = True
        else:
            root = Path(os.path.realpath(self.workspace / ".boxteam"))
            attachment_scope = False
        root_parts = tuple(map(os.path.normcase, root.parts))
        root_depth = len(root_parts)

        def is_private(target: Path) -> bool:
            # 分类保留循环/断链的 symlink 节点；真正访问仍经 resolve/safe_join 明确失败。
            resolved = Path(os.path.realpath(target))
            # 按路径段比较归属，避免逐 child 构造整条祖先 Path 链。
            target_parts = tuple(map(os.path.normcase, target.parts))
            resolved_parts = tuple(map(os.path.normcase, resolved.parts))
            if attachment_scope:
                return (target_parts[:root_depth] != root_parts or resolved_parts[:root_depth] != root_parts
                        or ".boxteam" in target_parts[root_depth:])
            return (".boxteam" in (*target_parts, *resolved_parts)
                    or resolved_parts[:root_depth] == root_parts)

        return root, is_private

    def is_private(self, target: Path, *, root_uri: str | None = None) -> bool:
        """单路径及长期事件流即时取根，使用与目录扫描相同的私有边界规则。"""
        _root, is_private = self._private_path_filter(root_uri=root_uri)
        return is_private(target)

    @contextmanager
    def private_path_filter(self, *, root_uri: str) -> Iterator[Callable[[Path], bool]]:
        """只在一次同步目录扫描内复用根，结束前重核，不跨请求缓存。"""
        # URI、附件 catalog locator 和私有根不应为每个 child 重复解析。
        # child 的 realpath 仍逐项核验；收尾重核根，拒绝扫描期间发生边界改写的结果。
        root, is_private = self._private_path_filter(root_uri=root_uri)
        yield is_private
        current_root, _is_private = self._private_path_filter(root_uri=root_uri)
        if current_root != root:
            raise ForbiddenError("目录扫描期间私有存储边界发生改变，请重新读取")

    def require_public_path(self, target: Path) -> None:
        if self.is_private(target):
            raise ForbiddenError("私有存储只允许由业务 owner 访问")

    def require_access(self, target: Path, *, root_uri: str, for_write: bool = False) -> None:
        """原生私有路径不因附件可访问而放开；授权始终保留明确 URI 根。"""
        if parse_resource_uri(root_uri).kind != "attachments":
            self.require_public_path(target)
            return
        root, _ = self.resolve(root_uri)
        if not target.is_relative_to(root):
            raise ForbiddenError("文件节点越出明确的附件根")
        uri = resource_uri.file_uri_child(root_uri, *target.relative_to(root).parts)
        resolved, _ = self.resolve(uri, for_write=for_write)
        if resolved != target:
            raise ForbiddenError("文件节点与附件 URI 的目标不一致")

    def resolve(self, value: str, *, scope: str = "workspace", allow_empty: bool = True,
                reject_symlink: bool = False, allow_external: bool = True, for_write: bool = False,
                materialize: bool = True, require_directory: bool = False) -> tuple[Path, str]:
        if is_resource_uri_input(value):
            parsed = parse_resource_uri(value, local_gateway_id=get_current_gateway_id())
            if parsed.kind == "attachments":
                if self.attachments is None:
                    raise RuntimeError("文件 owner 尚未装配附件目录解析")
                if any(os.path.normcase(part) == ".boxteam" for part in parsed.tail_segments[1:]):
                    raise ForbiddenError("附件目录不公开嵌套的私有存储")
                return self.attachments.resolve_directory_uri(parsed, for_write=for_write, materialize=materialize,
                                                              require_directory=require_directory), parsed.display_uri
            if parsed.kind != "files" or parsed.scope not in {"workspace", "gateway"}:
                raise ValueError("文件入口必须指向 Workspace 或 Gateway files 资源")
            if parsed.gateway_authority is not None:
                raise ValueError("文件 URI 的 Gateway authority 与目标机器不符")
            with file_access(parsed.display_uri, "目标解析"):
                if parsed.scope == "workspace":
                    if parsed.scope_id != self.workspace_id:
                        raise ValueError("文件 URI 的工作区与固定目标不符")
                    root = self.workspace
                else:
                    root = self._bindings().resolve(parsed)
                self.require_public_path(root.joinpath(*parsed.tail_segments[1:]))
                if reject_symlink and native_io_path(root.joinpath(*parsed.tail_segments[1:])).is_symlink():
                    raise ValueError(f"不允许编辑符号链接: {parsed.display_uri}")
                target = safe_join(root, *parsed.tail_segments[1:])
                self.require_public_path(target)
                return target, self.canonical_uri(parsed, root)
        if not value and not allow_empty:
            raise ValueError("文件路径不能为空")
        with file_access(None, "目标解析"):
            native = Path(value).expanduser()
            unresolved = native if native.is_absolute() else self.workspace / native
            self.require_public_path(unresolved)
            if reject_symlink and native_io_path(unresolved).is_symlink():
                raise ValueError("不允许通过文件预览编辑符号链接")
            if not native.is_absolute() and scope == "filesystem":
                if value:
                    raise ValueError("原生目录入口必须是绝对路径或资源 URI")
                target = Path(self.workspace.anchor)
            else:
                target = resolve_native_file_path(self.workspace, value, allow_absolute_outside=allow_external)
            return target, self.reference(target)

    def reference(self, target: Path, *, root_uri: str | None = None) -> str:
        if root_uri is not None and parse_resource_uri(root_uri).kind == "attachments":
            self.require_access(target, root_uri=root_uri)
            root, _ = self.resolve(root_uri)
            return resource_uri.file_uri_child(root_uri, *target.relative_to(root).parts)
        self.require_public_path(target)
        if target.is_relative_to(self.workspace):
            return resource_uri.file_uri_child(self.workspace_uri, *target.relative_to(self.workspace).parts)
        root = target if native_io_path(target).is_dir() else target.parent
        uri = self._bindings().bind(root)
        return uri if root == target else resource_uri.file_uri_child(uri, target.name)

    def native(self, uri: str, *, for_write: bool = False, materialize: bool = True) -> Path:
        return self.resolve(uri, for_write=for_write, materialize=materialize)[0]
