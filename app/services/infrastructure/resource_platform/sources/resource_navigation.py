"""已公布文件根的轻量导航；不登记来源、快照或上下文激活。"""

from __future__ import annotations

import codecs
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.distribution_identity import load_distribution_id
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

if TYPE_CHECKING:
    from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
        GatewaySkillSources,
    )


def _resolve_path(path: Path) -> Path:
    try:
        return path.resolve()
    except RuntimeError as error:
        raise ResourceUriResolveError("capability_denied", "资源路径包含符号链接循环") from error


class ResourceNavigation:
    """Workspace 与 Gateway 共用一个文件访问实现，根绑定只来自显式 owner。"""

    def __init__(
        self, *, workspace_root: Path | None = None, workspace_id: str | None = None,
        project_root: Path | None = None, gateway_sources: GatewaySkillSources | None = None,
        gateway_skills_root: Path | None = None, gateway_id: str | None = None,
    ) -> None:
        if (workspace_root is None) == (gateway_skills_root is None):
            raise ValueError("导航 owner 必须且只能绑定 Workspace 或 Gateway Skill 域")
        if workspace_root is not None and not workspace_id:
            raise ValueError("Workspace 导航必须显式绑定 workspace_id")
        if gateway_skills_root is not None and not gateway_id:
            raise ValueError("Gateway Skill 导航必须显式绑定 gateway_id")
        self._workspace_root = _resolve_path(workspace_root) if workspace_root is not None else None
        self._workspace_id = workspace_id
        self._project_root = _resolve_path(project_root or Path.cwd())
        self._gateway_sources = gateway_sources
        self._gateway_skills_root = gateway_skills_root.absolute() if gateway_skills_root is not None else None
        self._gateway_id = gateway_id

    async def navigate(
        self, request: ResourceNavigationRequest, *, local_gateway_id: str,
    ) -> ResourceNavigationResultDTO:
        parsed = parse_resource_uri(request.uri, local_gateway_id=local_gateway_id)
        if parsed.gateway_authority is not None:
            raise ResourceUriResolveError("scope_mismatch", "此入口只解析固定本机 Gateway 的资源")
        if self._gateway_id is not None and local_gateway_id != self._gateway_id:
            raise ResourceUriResolveError("scope_mismatch", "Gateway Skill 导航 owner 身份不一致")
        distribution_id = load_distribution_id() if parsed.scope == "inline" else None
        require_scope_binding(parsed.scope, parsed.scope_id, ResolutionContext(
            workspace_id=self._workspace_id, gateway_id=local_gateway_id,
            distribution_id=distribution_id,
        ))
        if parsed.scope == "gateway" and self._workspace_root is not None:
            if parsed.kind != "skills" or self._gateway_sources is None:
                raise ResourceUriResolveError("unknown_resource", "没有对应的 Gateway 文件 owner")
            return await self._gateway_sources.navigate(
                request.model_copy(update={"uri": parsed.display_uri}), gateway_id=local_gateway_id,
            )
        root, children = self._root(parsed)
        path = self._contained(root, children, parsed)
        node = self._node(parsed, path)
        if request.operation not in node.operations:
            raise ResourceUriResolveError("capability_denied", "资源不允许所请求的导航操作")
        if request.operation == "describe":
            return ResourceNavigationResultDTO(node=node)
        if request.operation == "read":
            # 普通读取有界且不 hash、不经过来源 reconciler，也不产生 activation。
            with path.open("rb") as stream:
                data = stream.read(request.max_bytes + 1)
            truncated = len(data) > request.max_bytes
            content_bytes = data[:request.max_bytes]
            # 有界窗口可能在 UTF-8 字符内部结束；增量 decoder 只保留完整字符，非法正文仍失败。
            content = codecs.getincrementaldecoder("utf-8")().decode(content_bytes, final=not truncated)
            return ResourceNavigationResultDTO(node=node, content=content, truncated=truncated)
        entries: list[ResourceNavigationNodeDTO] = []
        pending = [children]
        visited: set[Path] = set()
        while pending:
            relative = pending.pop()
            directory = self._contained(root, relative, parsed)
            if directory in visited:
                continue
            visited.add(directory)
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
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
                if request.operation == "list" or request.query in "/".join(child_parts):
                    if len(entries) == request.limit:
                        return ResourceNavigationResultDTO(node=node, entries=tuple(entries), truncated=True)
                    entries.append(child_node)
                if request.operation == "search" and child_node.resource_shape == "directory":
                    pending.append(child_parts)
            if request.operation == "list":
                break
        return ResourceNavigationResultDTO(node=node, entries=tuple(entries))

    def _root(self, parsed: ParsedResourceUri) -> tuple[Path, tuple[str, ...]]:
        if parsed.kind == "files" and self._workspace_root is not None:
            return self._workspace_root, parsed.tail_segments[1:]
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
