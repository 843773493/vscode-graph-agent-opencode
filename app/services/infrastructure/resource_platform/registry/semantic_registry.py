"""语义 ResourceRegistry:published immutable snapshot 的唯一事实面。

registry 只管理语义资源 identity/display URI/来源绑定(lineage)与已发布
快照;locator/handle 归实际 owner 私有,registry 不持有任何物理路径。
发布采用 CAS:同 revision 幂等返回已存快照;进程重启后由 owner 重新注册
descriptor 并重新派生,revision 由 payload hash 决定而天然稳定。
"""

from __future__ import annotations

from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)


class ResourceRegistry:
    """语义资源的 published revision/snapshot registry。"""

    def __init__(self) -> None:
        self._descriptors: dict[str, SemanticResourceDescriptor] = {}
        self._snapshots: dict[str, ResourceSnapshot] = {}
        self._last_valid: dict[str, ResourceSnapshot] = {}

    def register_descriptor(
        self, descriptor: SemanticResourceDescriptor
    ) -> None:
        """登记或恢复语义资源描述;同 id 幂等,冲突显式失败。"""
        if not isinstance(descriptor, SemanticResourceDescriptor):
            raise TypeError(
                "ResourceRegistry.register_descriptor 需要 SemanticResourceDescriptor"
            )
        existing = self._descriptors.get(descriptor.resource_id)
        if existing is not None and existing != descriptor:
            raise ValueError(
                f"语义资源描述冲突: resource_id={descriptor.resource_id}"
            )
        self._descriptors[descriptor.resource_id] = descriptor

    def descriptor(self, resource_id: str) -> SemanticResourceDescriptor:
        descriptor = self._descriptors.get(resource_id)
        if descriptor is None:
            raise KeyError(f"语义资源尚未注册: resource_id={resource_id}")
        return descriptor

    def descriptors(self) -> tuple[SemanticResourceDescriptor, ...]:
        return tuple(self._descriptors.values())

    def snapshots(self) -> tuple[ResourceSnapshot, ...]:
        """返回当前全部已发布快照；缺失快照由调用方显式处理。"""

        return tuple(self._snapshots.values())

    def resource_kinds(self) -> frozenset[str]:
        """返回已登记的 resource kind 闭集，供 activation policy 校验。"""

        return frozenset(descriptor.resource_kind for descriptor in self._descriptors.values())

    def publish(self, snapshot: ResourceSnapshot) -> ResourceSnapshot:
        """CAS 发布:同 revision 幂等;失败保留旧 valid 并显式 unavailable。"""
        if not isinstance(snapshot, ResourceSnapshot):
            raise TypeError("ResourceRegistry.publish 需要 ResourceSnapshot")
        existing_descriptor = self._descriptors.get(snapshot.resource_id)
        if existing_descriptor is None:
            self.register_descriptor(
                SemanticResourceDescriptor(
                    resource_id=snapshot.resource_id,
                    resource_kind=snapshot.resource_kind,
                    facet=snapshot.facet,
                    display_uri=snapshot.display_uri,
                )
            )
        elif (
            existing_descriptor.facet != snapshot.facet
            or existing_descriptor.display_uri != snapshot.display_uri
        ):
            raise ValueError(
                "快照与登记描述不一致: "
                + f"resource_id={snapshot.resource_id}",
            )
        existing = self._snapshots.get(snapshot.resource_id)
        if (
            existing is not None
            and existing.revision == snapshot.revision
            and existing.available == snapshot.available
        ):
            return existing
        self._snapshots[snapshot.resource_id] = snapshot
        if snapshot.available:
            self._last_valid[snapshot.resource_id] = snapshot
        return snapshot

    def snapshot(self, resource_id: str) -> ResourceSnapshot:
        snapshot = self._snapshots.get(resource_id)
        if snapshot is None:
            raise KeyError(f"语义资源尚未发布: resource_id={resource_id}")
        return snapshot

    def last_valid_snapshot(self, resource_id: str) -> ResourceSnapshot:
        """最近一次 available 快照;unavailable 期间的旧 valid 仍可审计。"""
        snapshot = self._last_valid.get(resource_id)
        if snapshot is None:
            raise KeyError(f"语义资源从未有效发布: resource_id={resource_id}")
        return snapshot

    def latest_revision(self, resource_id: str) -> str | None:
        snapshot = self._snapshots.get(resource_id)
        if snapshot is None:
            return None
        return snapshot.revision or snapshot.retained_revision
