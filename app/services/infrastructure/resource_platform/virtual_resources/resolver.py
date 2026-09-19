"""VRN typed resolver：在任何 owner/provider 访问前完成全部拒绝校验。

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

from app.services.infrastructure.resource_platform.virtual_resources.grammar import (
    parse_vrn,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ALL_OPERATIONS,
    ResolutionContext,
    ResolvedResourceHandle,
    ResourceProvenance,
    SemanticResourceDescriptor,
)

_RESOLVE_REASON_CODES = frozenset(
    {
        "scope_mismatch",
        "unknown_resource",
        "unknown_operation",
        "capability_denied",
        "snapshot_unavailable",
        "historical_snapshot_missing",
    }
)


class VrnResolveError(RuntimeError):
    """resolver 显式拒绝；reason_code 是闭合集合。"""

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in _RESOLVE_REASON_CODES:
            raise ValueError(f"未知 VrnResolveError reason_code: {reason_code}")
        super().__init__(message)
        self.reason_code = reason_code


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
class VirtualResourceCatalog:
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
            raise VrnResolveError(
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


class VirtualResourceResolver:
    """按 grammar → scope → catalog → operation → capability 顺序拒绝的纯门卫。"""

    def __init__(self, catalog: VirtualResourceCatalog) -> None:
        self._catalog = catalog

    def resolve(
        self,
        uri: str,
        *,
        operation: str,
        context: ResolutionContext,
    ) -> ResolvedResourceHandle:
        parsed = parse_vrn(uri)
        self._check_scope(parsed.scope, parsed.scope_id, context)
        try:
            binding = self._catalog.bindings[uri]
        except KeyError as error:
            raise VrnResolveError(
                "unknown_resource", f"VRN 未在当前 catalog 登记: {uri!r}"
            ) from error
        if operation not in ALL_OPERATIONS:
            raise VrnResolveError(
                "unknown_operation", f"VRN operation 未登记: {operation!r}"
            )
        if operation not in binding.capabilities:
            raise VrnResolveError(
                "capability_denied",
                f"VRN 绑定不具备 operation {operation!r}: uri={uri!r}",
            )
        if binding.snapshot_ref is None:
            raise VrnResolveError(
                "snapshot_unavailable", f"VRN 绑定当前快照不可用: uri={uri!r}"
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
            raise VrnResolveError(
                "unknown_resource",
                f"名称未在当前 catalog 登记: scope={scope!r} name={logical_name!r}",
            ) from error
        return self.resolve(uri, operation=operation, context=context)

    @staticmethod
    def _check_scope(
        scope: str, scope_id: str, context: ResolutionContext
    ) -> None:
        bound = {
            "workspace": context.workspace_id,
            "gateway": context.gateway_id,
            "builtin": context.distribution_id,
        }.get(scope)
        if scope == "memory":
            # memory 是 workspace 业务数据，必须存在 workspace principal 绑定。
            if context.workspace_id is None:
                raise VrnResolveError(
                    "scope_mismatch", "memory VRN 需要 workspace principal 绑定"
                )
            return
        if bound != scope_id:
            raise VrnResolveError(
                "scope_mismatch",
                f"VRN scope 与 principal 绑定不一致: scope={scope!r} "
                f"uri_id={scope_id!r} bound={bound!r}",
            )
