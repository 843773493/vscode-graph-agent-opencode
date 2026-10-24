"""URI typed resolver：在任何 owner/provider 访问前完成全部拒绝校验。

resolver 是纯门卫：输入 URI、operation 与 principal 绑定上下文，查注入的
catalog 快照，产出 typed handle。它不读文件、不访问网络、不触碰 memory
正文，也不提供 plugin/provider 装配或动态 import。scope/capability 错配、
伪造与越界 URI 一律显式报错，错误码闭合。

历史合同：tracked 绑定在绑定时冻结 resource/revision/hash/snapshot_ref；
同名覆盖只改变未来名称解析，既有 registration 继续绑定原 resource id，
历史恢复只使用封存值，不按当前 catalog 或 display URI 重解。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.core.resource_uri import (
    ParsedResourceUri,
    parse_resource_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ALL_OPERATIONS,
    ResolutionContext,
    ResolvedResourceHandle,
    ResourceNavigationNode,
    ResourceProvenance,
    ResourceShape,
    ResourceTargetAction,
    ResourceTargetContext,
    ResourceTargetDecision,
    ResourceTargetFacts,
    SemanticResourceDescriptor,
)


def _has_target_retention(facts: ResourceTargetFacts, target: ResourceTargetContext) -> bool:
    """保留范围由 owner 给出，URI scope 不参与生命周期推导。"""
    return facts.lifecycle == "workspace" or facts.retention_scope == "workspace" or (
        facts.retention_scope == "session" and facts.owner_session_id is not None
        and facts.owner_session_id == target.owner_session_id
    ) or (
        facts.retention_scope == "thread" and facts.owner_session_id is not None
        and facts.owner_session_id == target.owner_session_id
        and facts.owner_thread_id is not None and facts.owner_thread_id == target.owner_thread_id
    )


def check_resource_target(
    facts: ResourceTargetFacts,
    target: ResourceTargetContext,
    *,
    choice: ResourceTargetAction | None = None,
) -> ResourceTargetDecision:
    """单点决定引用处理；副作用仍由提供事实的原领域 owner 执行。"""
    if choice == "omit":
        if facts.required_for_context:
            raise ValueError(f"context_required_resource: 必需正文不能遗漏: {facts.resource_uri}")
        return ResourceTargetDecision(facts.resource_uri, "omit", False, "用户未继承该资源")
    same_domain = facts.storage_domain_id == target.storage_domain_id
    if facts.lifecycle == "runtime":
        runtime = facts.runtime
        if runtime is None or facts.resource_uri is not None:
            raise ValueError("运行资源目标检查缺少 typed owner 事实")
        if choice is None:
            return ResourceTargetDecision(None, "omit", False, "运行资源默认不继承", runtime=runtime)
        reason = None
        if choice != "reuse_reference":
            reason = "运行资源只能复用原身份与实例"
        elif not same_domain or runtime.workspace_id != target.workspace_id:
            reason = "跨工作区不支持复制 live Terminal/Browser 实例"
        elif runtime.retention_scope != "workspace" and runtime.source_thread != (target.owner_session_id, target.owner_thread_id):
            reason = "thread 范围资源不能关联其它 Thread；fork 不自动 promote"
        elif not facts.available or runtime.reconciliation_status != "verified" or "attach" not in runtime.available_actions:
            reason = "原 owner 未核实资源或不允许 attach"
        return ResourceTargetDecision(None, "unsupported" if reason else "reuse_reference", reason is None, reason, runtime=runtime)
    existing = target.existing_reference
    if (
        existing is not None and (same_domain or choice is not None or facts.required_for_context)
        and existing.available and existing.lifecycle != "runtime" and _has_target_retention(existing, target)
        and existing.storage_domain_id == target.storage_domain_id
        and facts.revision is not None and facts.content_hash is not None and facts.content_length is not None
        and (existing.revision, existing.content_hash, existing.content_length)
        == (facts.revision, facts.content_hash, facts.content_length)
    ):
        return ResourceTargetDecision(facts.resource_uri, "reuse_reference", True, target_uri=existing.resource_uri)
    if not facts.available:
        if facts.required_for_context:
            raise FileNotFoundError(f"context_required_resource_unavailable: {facts.resource_uri}")
        return ResourceTargetDecision(facts.resource_uri, "omit", False, "来源资源不可用")
    if same_domain and _has_target_retention(facts, target):
        if choice not in {None, "reuse_reference", "retain_reference", "copy_snapshot"}:
            raise ValueError(f"resource_target_action_mismatch: 资源 owner 要求 reuse_reference: {facts.resource_uri}")
        return ResourceTargetDecision(facts.resource_uri, "reuse_reference", True, target_uri=facts.resource_uri)
    if facts.lifecycle == "snapshot" or not same_domain:
        if not facts.required_for_context and choice is None:
            return ResourceTargetDecision(facts.resource_uri, "omit", False, "跨存储域资源未选择继承")
        if not (facts.snapshot_import_supported and target.snapshot_import_supported):
            if facts.required_for_context:
                raise ValueError(f"context_required_resource_unsupported: {facts.resource_uri}")
            return ResourceTargetDecision(facts.resource_uri, "unsupported", False, "目标不支持资源快照导入")
        if choice not in {None, "copy_snapshot"}:
            raise ValueError(f"resource_target_action_mismatch: 跨域资源必须选择快照导入: {facts.resource_uri}")
        return ResourceTargetDecision(facts.resource_uri, "copy_snapshot", True)
    action: ResourceTargetAction = "retain_reference"
    if choice not in {None, action, "copy_snapshot"}:
        raise ValueError(f"resource_target_action_mismatch: 资源 owner 要求 {action}: {facts.resource_uri}")
    return ResourceTargetDecision(facts.resource_uri, action, True)

RESOURCE_URI_RESOLVE_REASON_CODES = frozenset(
    {
        "scope_mismatch",
        "unknown_resource",
        "unknown_operation",
        "capability_denied",
        "snapshot_unavailable",
        "historical_snapshot_missing",
        "unsupported_view",
    }
)


class ResourceUriResolveError(RuntimeError):
    """resolver 显式拒绝；reason_code 是闭合集合。"""

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in RESOURCE_URI_RESOLVE_REASON_CODES:
            raise ValueError(f"未知 ResourceUriResolveError reason_code: {reason_code}")
        super().__init__(message)
        self.reason_code = reason_code


def resource_navigation_node(
    parsed: ParsedResourceUri, *, resource_shape: ResourceShape,
) -> ResourceNavigationNode:
    """固定根模板的唯一导航合同；实际节点 shape 由持有文件的 owner 核实。"""
    if parsed.kind in {"skills", "files", "attachments"}:
        if len(parsed.tail_segments) == 1 and resource_shape != "directory":
            raise ResourceUriResolveError("capability_denied", "已公布的资源根必须是目录")
        if resource_shape not in {"directory", "file"}:
            raise ResourceUriResolveError("capability_denied", "文件树节点不能作为领域对象")
    elif parsed.kind in {"agent-spec", "config"} or parsed.session_artifact_kind is not None:
        if resource_shape != "file" or (parsed.kind == "config" and len(parsed.tail_segments) != 1):
            raise ResourceUriResolveError("capability_denied", "单文件引用不允许追加子路径")
    elif resource_shape != "domain_object":
        raise ResourceUriResolveError("capability_denied", "领域资源不公开私有文件树")
    operations = (
        ("describe", "list", "search") if resource_shape == "directory"
        else ("describe", "read", "search") if resource_shape == "file" and (parsed.kind in {"files", "skills", "attachments"} or parsed.session_artifact_kind == "tool-results")
        else ("describe", "read") if resource_shape == "file" else ("describe",)
    )
    return ResourceNavigationNode(
        uri=parsed.display_uri, resource_shape=resource_shape,
        operations=operations,
        child_path="file_segments" if resource_shape == "directory" else None,
    )


@dataclass(frozen=True, slots=True)
class CatalogBinding:
    """catalog 快照内一条 display URI 绑定；snapshot_ref 缺失表示快照不可用。"""

    descriptor: SemanticResourceDescriptor
    capabilities: frozenset[str]
    snapshot_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, SemanticResourceDescriptor):
            raise TypeError("CatalogBinding.descriptor 必须是 SemanticResourceDescriptor")
        if not isinstance(self.capabilities, frozenset):
            raise TypeError("CatalogBinding.capabilities 必须是 frozenset")
        unknown = self.capabilities - ALL_OPERATIONS
        if unknown:
            raise ValueError(f"CatalogBinding.capabilities 未登记: {unknown!r}")
        if self.snapshot_ref is not None:
            if not isinstance(self.snapshot_ref, str) or not self.snapshot_ref:
                raise ValueError("CatalogBinding.snapshot_ref 必须是非空字符串或 None")
            if any(
                marker in self.snapshot_ref for marker in ("/", "\\", "@", "%")
            ):
                raise ValueError(
                    f"CatalogBinding.snapshot_ref 不得是路径/URI/credential 形态: {self.snapshot_ref!r}"
                )


@dataclass(frozen=True, slots=True)
class ResourceUriCatalog:
    """当前 catalog 快照：display_uri 绑定与逻辑名称索引。"""

    bindings: Mapping[str, CatalogBinding]
    name_index: Mapping[tuple[str, str], str]

    def __post_init__(self) -> None:
        for uri in self.name_index.values():
            if uri not in self.bindings:
                raise ValueError(
                    f"name_index 指向未登记绑定: {uri!r}"
                )


@dataclass(frozen=True, slots=True)
class TrackedResourceBinding:
    """tracked registration 在绑定时冻结的不可变事实。

    之后 catalog 同名覆盖、locator 移动或 revision 更新都不得改变本绑定；
    历史恢复只使用这里封存的 resource/revision/hash/snapshot_ref。
    """

    name: str
    binding: CatalogBinding

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("TrackedResourceBinding.name 必须是非空字符串")
        if not isinstance(self.binding, CatalogBinding):
            raise TypeError("TrackedResourceBinding.binding 必须是 CatalogBinding")

    def resolve_frozen(self) -> ResolvedResourceHandle:
        """从封存事实恢复 handle；不查当前 catalog，不重解 display URI。"""
        if self.binding.snapshot_ref is None:
            raise ResourceUriResolveError(
                "historical_snapshot_missing",
                f"tracked 绑定缺失封存 snapshot_ref，无法恢复历史资源: name={self.name!r}",
            )
        descriptor = self.binding.descriptor
        return ResolvedResourceHandle(
            descriptor=descriptor,
            snapshot_ref=self.binding.snapshot_ref,
            capabilities=self.binding.capabilities,
            provenance=ResourceProvenance(
                display_uri=descriptor.display_uri,
                resource_id=descriptor.resource_id,
                source_id=descriptor.source_id,
                semantic_revision=descriptor.semantic_revision,
                semantic_hash=descriptor.semantic_hash,
            ),
        )


def require_scope_binding(
    scope: str, scope_id: str, context: ResolutionContext
) -> None:
    """校验 URI scope 与该 principal 绑定一致；不一致显式结构化拒绝。"""
    bound = {
        "workspace": context.workspace_id,
        "user": context.user_scope_id,
        "gateway": context.gateway_id,
        "inline": context.distribution_id,
    }.get(scope)
    if bound != scope_id:
        raise ResourceUriResolveError(
            "scope_mismatch",
            f"URI scope 与 principal 绑定不一致: scope={scope!r} "
            f"uri_id={scope_id!r} bound={bound!r}",
        )


class ResourceUriResolver:
    """按 grammar → scope → catalog → operation → capability 顺序拒绝的纯门卫。"""

    def __init__(self, catalog: ResourceUriCatalog) -> None:
        self._catalog = catalog

    def resolve(
        self,
        uri: str,
        *,
        operation: str,
        context: ResolutionContext,
    ) -> ResolvedResourceHandle:
        parsed = parse_resource_uri(uri, local_gateway_id=context.gateway_id)
        if parsed.gateway_authority is not None:
            raise ResourceUriResolveError("scope_mismatch", "远端资源 URI 必须经 Gateway 的原联邦 owner 解析")
        uri = parsed.display_uri
        self._check_scope(parsed.scope, parsed.scope_id, context)
        try:
            binding = self._catalog.bindings[uri]
        except KeyError as error:
            raise ResourceUriResolveError(
                "unknown_resource", f"URI 未在当前 catalog 登记: {uri!r}"
            ) from error
        if operation not in ALL_OPERATIONS:
            raise ResourceUriResolveError(
                "unknown_operation", f"URI operation 未登记: {operation!r}"
            )
        if operation not in binding.capabilities:
            raise ResourceUriResolveError(
                "capability_denied",
                f"URI 绑定不具备 operation {operation!r}: uri={uri!r}",
            )
        if binding.snapshot_ref is None:
            raise ResourceUriResolveError(
                "snapshot_unavailable", f"URI 绑定当前快照不可用: uri={uri!r}"
            )
        descriptor = binding.descriptor
        return ResolvedResourceHandle(
            descriptor=descriptor,
            snapshot_ref=binding.snapshot_ref,
            capabilities=binding.capabilities,
            provenance=ResourceProvenance(
                display_uri=descriptor.display_uri,
                resource_id=descriptor.resource_id,
                source_id=descriptor.source_id,
                semantic_revision=descriptor.semantic_revision,
                semantic_hash=descriptor.semantic_hash,
            ),
        )

    def resolve_name(
        self,
        *,
        scope: str,
        logical_name: str,
        operation: str,
        context: ResolutionContext,
    ) -> ResolvedResourceHandle:
        """按当前 catalog 的名称索引解析；同名覆盖只影响未来调用。"""
        try:
            uri = self._catalog.name_index[(scope, logical_name)]
        except KeyError as error:
            raise ResourceUriResolveError(
                "unknown_resource",
                f"名称未在当前 catalog 登记: scope={scope!r} name={logical_name!r}",
            ) from error
        return self.resolve(uri, operation=operation, context=context)

    @staticmethod
    def _check_scope(
        scope: str, scope_id: str, context: ResolutionContext
    ) -> None:
        require_scope_binding(scope, scope_id, context)
