"""语义 ResourceRegistry：published immutable snapshot 的唯一事实面。

registry 只管理语义资源 identity/display URI/来源绑定与已发布快照；
locator/handle 归实际 owner 私有。完整发布值相同才幂等返回既有快照，
成员、来源或代际变化不改写 payload 的语义 revision。
"""

from __future__ import annotations

from _thread import RLock
from collections.abc import Callable
from dataclasses import dataclass

from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)


@dataclass(frozen=True, slots=True)
class ResourceRegistryCapture:
    """同一次读锁内捕获的描述、发布快照与资源种类闭集。"""

    descriptors: tuple[SemanticResourceDescriptor, ...]
    snapshots: tuple[ResourceSnapshot, ...]
    resource_kinds: frozenset[str]


class ResourceRegistry:
    """语义资源的 published revision/snapshot registry。"""

    def __init__(
        self,
        *,
        publication_lock: RLock | None = None,
        capture_validator: Callable[[ResourceRegistryCapture], None] | None = None,
    ) -> None:
        # MCP Owner 和全部 runtime Registry 共用这唯一一把发布锁。
        self._lock = publication_lock if publication_lock is not None else RLock()
        self._capture_validator = capture_validator
        self._descriptors: dict[str, SemanticResourceDescriptor] = {}
        self._snapshots: dict[str, ResourceSnapshot] = {}
        self._last_valid: dict[str, ResourceSnapshot] = {}

    def register_descriptor(
        self, descriptor: SemanticResourceDescriptor
    ) -> None:
        """登记或恢复语义资源描述；同 id 幂等，冲突显式失败。"""
        if not isinstance(descriptor, SemanticResourceDescriptor):
            raise TypeError(
                "ResourceRegistry.register_descriptor 需要 SemanticResourceDescriptor"
            )
        with self._lock:
            existing = self._descriptors.get(descriptor.resource_id)
            if existing is not None and existing != descriptor:
                raise ValueError(
                    f"语义资源描述冲突: resource_id={descriptor.resource_id}"
                )
            self._descriptors[descriptor.resource_id] = descriptor

    def descriptor(self, resource_id: str) -> SemanticResourceDescriptor:
        with self._lock:
            descriptor = self._descriptors.get(resource_id)
            if descriptor is None:
                raise KeyError(f"语义资源尚未注册: resource_id={resource_id}")
            return descriptor

    def capture(self) -> ResourceRegistryCapture:
        """原子读取完整发布面，调用方必须消费本次捕获值完成校验与冻结。"""
        with self._lock:
            descriptors = tuple(self._descriptors.values())
            capture = ResourceRegistryCapture(
                descriptors=descriptors,
                snapshots=tuple(self._snapshots.values()),
                resource_kinds=frozenset(item.resource_kind for item in descriptors),
            )
            if self._capture_validator is not None:
                self._capture_validator(capture)
            return capture

    def descriptors(self) -> tuple[SemanticResourceDescriptor, ...]:
        with self._lock:
            return tuple(self._descriptors.values())

    def snapshots(self) -> tuple[ResourceSnapshot, ...]:
        """返回当前全部已发布快照；组合读取使用 capture。"""
        with self._lock:
            return tuple(self._snapshots.values())

    def resource_kinds(self) -> frozenset[str]:
        """返回已登记的 resource kind 闭集，供 activation policy 校验。"""
        with self._lock:
            return frozenset(
                descriptor.resource_kind for descriptor in self._descriptors.values()
            )

    def publish(self, snapshot: ResourceSnapshot) -> ResourceSnapshot:
        """CAS 发布完整不可变值；同语义 revision 的成员变化仍须发布。"""
        if not isinstance(snapshot, ResourceSnapshot):
            raise TypeError("ResourceRegistry.publish 需要 ResourceSnapshot")
        with self._lock:
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
                existing_descriptor.resource_kind != snapshot.resource_kind
                or existing_descriptor.facet != snapshot.facet
                or existing_descriptor.display_uri != snapshot.display_uri
            ):
                raise ValueError(
                    f"快照与登记描述不一致: resource_id={snapshot.resource_id}"
                )
            existing = self._snapshots.get(snapshot.resource_id)
            if existing == snapshot:
                return existing
            self._snapshots[snapshot.resource_id] = snapshot
            if snapshot.available:
                self._last_valid[snapshot.resource_id] = snapshot
            return snapshot

    def snapshot(self, resource_id: str) -> ResourceSnapshot:
        with self._lock:
            snapshot = self._snapshots.get(resource_id)
            if snapshot is None:
                raise KeyError(f"语义资源尚未发布: resource_id={resource_id}")
            return snapshot

    def last_valid_snapshot(self, resource_id: str) -> ResourceSnapshot:
        """最近一次 available 快照；unavailable 期间的旧 valid 仍可审计。"""
        with self._lock:
            snapshot = self._last_valid.get(resource_id)
            if snapshot is None:
                raise KeyError(f"语义资源从未有效发布: resource_id={resource_id}")
            return snapshot

    def withdraw(self, resource_id: str) -> None:
        """撤销已消失来源的当前发布面；已冻结正文与 last-valid 审计事实不变。"""
        with self._lock:
            self._descriptors.pop(resource_id, None)
            self._snapshots.pop(resource_id, None)

    def latest_revision(self, resource_id: str) -> str | None:
        with self._lock:
            snapshot = self._snapshots.get(resource_id)
            if snapshot is None:
                return None
            return snapshot.revision or snapshot.retained_revision
