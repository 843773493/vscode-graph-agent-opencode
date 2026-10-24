"""文件输入在目标机器归一；持久引用只使用统一资源 URI。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx

from app.core import resource_uri
from app.core.exceptions import ForbiddenError
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


class AttachmentDirectoryPaths(Protocol):
    def resolve_directory_uri(self, parsed: ParsedResourceUri, *, for_write: bool = False) -> Path: ...


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

    def _request(self, method: str, suffix: str, body: dict[str, str] | None = None) -> httpx.Response:
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
        return response

    def bind(self, root: Path) -> str:
        response = self._request("POST", "", {"native_root": str(root)})
        result = APIResponse[NativeDirectoryBindingDTO].model_validate_json(response.content)
        if result.code != 0 or result.data is None or result.request_id != response.headers["X-Request-ID"]:
            raise ValueError("原生根绑定控制链回执无效")
        parsed = parse_resource_uri(result.data.uri)
        if parsed.kind != "files" or parsed.scope != "gateway" or len(parsed.tail_segments) != 1 or parsed.gateway_authority is not None:
            raise ValueError("原生根绑定控制链返回了错误资源")
        return parsed.display_uri

    def resolve(self, parsed: ParsedResourceUri) -> Path:
        root_uri = resource_uri.file_uri_root(parsed.display_uri)
        response = self._request("GET", f"/{parsed.scope_id}/{parsed.tail_segments[0]}")
        result = APIResponse[NativeDirectoryResolveDTO].model_validate_json(response.content)
        if result.code != 0 or result.data is None or result.request_id != response.headers["X-Request-ID"] or result.data.uri != root_uri:
            raise ValueError("原生根解析控制链 owner 或 request_id 不一致")
        root = Path(result.data.native_root)
        if not root.is_absolute():
            raise ValueError("原生根解析控制链返回了相对路径")
        return root


class WorkspaceFilePaths:
    def __init__(self, *, workspace: Path, workspace_id: str, bindings: NativeDirectoryBindings | None,
                 attachments: AttachmentDirectoryPaths | None = None) -> None:
        self.workspace, self.workspace_id, self.bindings = workspace, workspace_id, bindings
        self.attachments = attachments
        self.workspace_uri = resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files", tail_segments=("root",))

    def _bindings(self) -> NativeDirectoryBindings:
        if self.bindings is None:
            raise RuntimeError("目标文件 owner 尚未装配原生 Gateway 目录根绑定")
        return self.bindings

    def is_private(self, target: Path, *, root_uri: str | None = None) -> bool:
        """普通文件树不公布保留存储目录；原生根和 symlink 别名遵守同一边界。"""
        # 分类保留循环/断链的 symlink 节点；真正访问仍经 resolve/safe_join 明确失败。
        resolved = Path(os.path.realpath(target))
        if root_uri is not None and parse_resource_uri(root_uri).kind == "attachments":
            root, _ = self.resolve(resource_uri.file_uri_root(root_uri))
            return (not target.is_relative_to(root) or not resolved.is_relative_to(root)
                    or any(os.path.normcase(part) == ".boxteam" for part in target.relative_to(root).parts))
        return (any(os.path.normcase(part) == ".boxteam" for part in (*target.parts, *resolved.parts))
                or resolved.is_relative_to(Path(os.path.realpath(self.workspace / ".boxteam"))))

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
                reject_symlink: bool = False, allow_external: bool = True, for_write: bool = False) -> tuple[Path, str]:
        if is_resource_uri_input(value):
            parsed = parse_resource_uri(value, local_gateway_id=get_current_gateway_id())
            if parsed.kind == "attachments":
                if self.attachments is None:
                    raise RuntimeError("文件 owner 尚未装配附件目录解析")
                if any(os.path.normcase(part) == ".boxteam" for part in parsed.tail_segments[1:]):
                    raise ForbiddenError("附件目录不公开嵌套的私有存储")
                return self.attachments.resolve_directory_uri(parsed, for_write=for_write), parsed.display_uri
            if parsed.kind != "files" or parsed.scope not in {"workspace", "gateway"}:
                raise ValueError("文件入口必须指向 Workspace 或 Gateway files 资源")
            if parsed.gateway_authority is not None:
                raise ValueError("文件 URI 的 Gateway authority 与目标机器不符")
            if parsed.scope == "workspace":
                if parsed.scope_id != self.workspace_id:
                    raise ValueError("文件 URI 的工作区与固定目标不符")
                root = self.workspace
            else:
                root = self._bindings().resolve(parsed)
            self.require_public_path(root.joinpath(*parsed.tail_segments[1:]))
            if reject_symlink and root.joinpath(*parsed.tail_segments[1:]).is_symlink():
                raise ValueError(f"不允许编辑符号链接: {parsed.display_uri}")
            target = safe_join(root, *parsed.tail_segments[1:])
            self.require_public_path(target)
            return target, parsed.display_uri
        if not value and not allow_empty:
            raise ValueError("文件路径不能为空")
        native = Path(value).expanduser()
        unresolved = native if native.is_absolute() else self.workspace / native
        self.require_public_path(unresolved)
        if reject_symlink and unresolved.is_symlink():
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
        root = target if target.is_dir() else target.parent
        uri = self._bindings().bind(root)
        return uri if root == target else resource_uri.file_uri_child(uri, target.name)

    def native(self, uri: str, *, for_write: bool = False) -> Path:
        return self.resolve(uri, for_write=for_write)[0]
