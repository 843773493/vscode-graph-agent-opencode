"""资源事件到 ContextSourceManager 的内存 reaction 接线。

本模块把来源 owner 的「某来源有新 revision」通知转换成一个待观察标记，并在
``before_model`` 的同步消费点用来源 owner 已发布的内存快照调用 CSM 的
``observe``。它不读文件、不写 ContextStore，也不构造第二个事件总线。

生命周期：reactor 持有唯一的 `resource.observe/*` 订阅与自己的
:class:`LifetimeScope`；释放时清空绑定并取消订阅，之后到达的事件不会再进入
CSM。释放失败经 scope 原样返回给持有它的 owner。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservation,
    ResourceObservationSubscription,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    PublishedContextSourceRevision,
)

logger = logging.getLogger(__name__)


class ResourceSnapshotLike(Protocol):
    """来源 owner 发布的权威内存快照；正文只存在于这里。"""

    @property
    def uri(self) -> str: ...

    @property
    def revision(self) -> str | None: ...

    @property
    def content(self) -> str: ...

    @property
    def available(self) -> bool: ...


class ResourceObservationSourcePort(Protocol):
    """reactor 需要的来源 owner 窄接口，不暴露物理 locator。"""

    def snapshot(self, uri: str) -> ResourceSnapshotLike: ...

    def subscribe_observation(
        self,
        *,
        label: str,
    ) -> ResourceObservationSubscription: ...

    def unsubscribe_observation(
        self,
        subscription: ResourceObservationSubscription,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class PublishedSourceObservationRef:
    """reactor 在 drain 时读取的 owner 已发布快照引用。

    只携带 source_id、resource_uri 和当时的 revision，不携带或冻结正文，
    也不代表 CSM 已 applied 或 durable committed；content_for 后续另取
    owner 的内存正文，不能把该引用当作正文与 revision 的冻结绑定。
    """

    source_id: str
    resource_uri: str
    revision: str


class ContextSourceReactor:
    """订阅来源变化并把权威快照送进 CSM 的 owner 侧接线。"""

    def __init__(
        self,
        *,
        sources: ResourceObservationSourcePort,
        context_sources: ContextSourceManager,
        lifetime_scope: LifetimeScope,
        reactor_id: str,
    ) -> None:
        if not isinstance(reactor_id, str) or not reactor_id.strip():
            raise ValueError("ContextSourceReactor.reactor_id 必须是非空字符串")
        self._sources = sources
        self._context_sources = context_sources
        self._reactor_id = reactor_id
        self._subscription: ResourceObservationSubscription | None = None
        self._source_ids_by_uri: dict[str, str] = {}
        self._tracking_statuses: dict[str, str] = {}
        self._released = False
        # 构建失败或尚未订阅也必须由同一 scope fence 掉整个 reactor。
        lifetime_scope.register(
            self._release_resources,
            label=f"context-source-reactor:{self._reactor_id}",
        )

    @property
    def reactor_id(self) -> str:
        return self._reactor_id

    @property
    def released(self) -> bool:
        return self._released

    @property
    def subscribed_uri_count(self) -> int:
        return len(self._source_ids_by_uri)

    def sync_sources(
        self, *, descriptors: tuple[ContextSourceDescriptor, ...] | None = None,
    ) -> tuple[str, ...]:
        """按 CSM 当前注册表增量接线，返回本次新绑定的虚拟 URI。

        该方法只做内存操作：为带 ``resource_uri`` 的 descriptor 建立订阅，
        并把「需要首次观察」的来源标记为待观察。
        """
        self._require_active()
        newly_bound: list[str] = []
        selected = self._context_sources.descriptors() if descriptors is None else descriptors
        for descriptor in selected:
            resource_uri = descriptor.resource_uri
            if resource_uri is None:
                # 非文件来源由对应 owner 直接调用 observe，不进入本 reactor。
                continue
            if resource_uri not in self._source_ids_by_uri:
                self._bind(descriptor, resource_uri)
                newly_bound.append(resource_uri)
            self._seed_if_needed(descriptor)
        return tuple(newly_bound)

    def ingest_notifications(self) -> int:
        """消费已排队的轻量通知，只登记待观察标记，不读取正文。

        返回本次新标记的**来源数量**（gap 会标记全部已绑定来源）；已释放时返回 0。
        """
        if self._released:
            return 0
        marked = 0
        for observation in self._pending_events():
            marked += self._handle_event(observation)
        return marked

    def drain(
        self,
        *,
        descriptors: tuple[ContextSourceDescriptor, ...] | None = None,
    ) -> Iterator[PublishedSourceObservationRef]:
        """把已排队的轻量通知转换为待观察来源，不读取任何来源内容。

        第一条 pending 到达时构造一次 descriptor 索引；调用方可复用请求边界快照，
        避免每条 pending 都重新扫描注册表。

        已释放的 reactor 返回空迭代器：订阅已经解除，事件不再到达 CSM；
        仍在执行的旧 agent 只保留它已有的上下文，不因为订阅释放而失败。
        """
        if self._released:
            return
        self.ingest_notifications()
        descriptors_by_source_id: dict[str, ContextSourceDescriptor] | None = None
        while True:
            pending = self._context_sources.next_pending_observation()
            if pending is None:
                return
            if descriptors_by_source_id is None:
                descriptors_by_source_id = {
                    descriptor.source_id: descriptor
                    for descriptor in (
                        self._context_sources.descriptors()
                        if descriptors is None
                        else descriptors
                    )
                }
            descriptor = descriptors_by_source_id.get(pending.source_id)
            if descriptor is None:
                raise KeyError(
                    "待观察来源尚未由 owner 重新注册 descriptor: "
                    f"source_id={pending.source_id}"
                )
            resource_uri = descriptor.resource_uri
            if resource_uri is None:
                raise RuntimeError(
                    "待观察来源缺少 resource_uri: "
                    f"source_id={pending.source_id}"
                )
            snapshot = self._sources.snapshot(resource_uri)
            if not snapshot.available:
                raise RuntimeError(
                    "来源快照不可用，无法消费 observation: "
                    f"source_id={pending.source_id} uri={resource_uri} "
                    f"revision={pending.revision}"
                )
            revision = snapshot.revision
            if revision is None:
                raise RuntimeError(
                    "来源快照缺少 revision: "
                    f"source_id={pending.source_id} uri={resource_uri}"
                )
            if pending.revision is not None and revision != pending.revision:
                # 事件与消费之间又发生了新变化；以权威内存快照为准，
                # 不用过期的观察 revision 覆盖更新的内容。
                logger.warning(
                    "来源 observation revision 已过期: source_id=%s expected=%s actual=%s",
                    pending.source_id,
                    pending.revision,
                    revision,
                )
            yield PublishedSourceObservationRef(
                source_id=pending.source_id,
                resource_uri=resource_uri,
                revision=revision,
            )

    def freeze_published_sources(
        self,
        descriptors: tuple[ContextSourceDescriptor, ...],
    ) -> tuple[PublishedContextSourceRevision, ...]:
        """复制来源 owner 已发布的精确内存 revision/body，不解析 locator 或读源。"""
        self._require_active()
        frozen = []
        for descriptor in descriptors:
            uri = descriptor.resource_uri
            if uri is None:
                raise ValueError("published resource 冻结缺少 resource_uri: " + descriptor.source_id)
            snapshot = self._sources.snapshot(uri)
            if not snapshot.available or not snapshot.revision:
                raise RuntimeError("tracked-source-unavailable: 已发布资源不可用: " + descriptor.source_id)
            frozen.append(PublishedContextSourceRevision(
                source_id=descriptor.source_id, revision=snapshot.revision, content=snapshot.content,
            ))
        return tuple(frozen)

    def content_for(self, observation: PublishedSourceObservationRef) -> str:
        """从来源 owner 的权威内存快照读取正文；不触发任何磁盘 I/O。"""
        snapshot = self._sources.snapshot(observation.resource_uri)
        if not snapshot.available:
            raise RuntimeError(
                "来源快照不可用: "
                f"uri={observation.resource_uri} "
                f"revision={observation.revision}"
            )
        return snapshot.content

    async def close(self) -> None:
        """释放订阅；释放后事件不再到达 CSM。"""
        await self._release_resources()

    def _pending_events(self) -> tuple[ResourceObservation, ...]:
        subscription = self._subscription
        if subscription is None:
            return ()
        return subscription.pending()

    def _bind(
        self,
        descriptor: ContextSourceDescriptor,
        resource_uri: str,
    ) -> None:
        if self._subscription is None:
            self._subscription = self._sources.subscribe_observation(
                label=f"context-source-reactor:{self._reactor_id}"
            )
        self._source_ids_by_uri[resource_uri] = descriptor.source_id
        logger.debug(
            "ContextSourceReactor 绑定来源: reactor=%s source_id=%s uri=%s",
            self._reactor_id,
            descriptor.source_id,
            resource_uri,
        )

    def _seed_if_needed(self, descriptor: ContextSourceDescriptor) -> None:
        """首次观察或 tracking 状态变化时，标记来源需要一次权威快照读取。

        恢复的 revision 不代表当前 owner 已观察来源；首绑必须对账，以补齐
        冷启动正文并接收订阅建立前发布的变化。正文与 revision 只在 drain 消费。
        """
        tracking_status, _latest_revision = (
            self._context_sources.source_observation_state(descriptor.source_id)
        )
        if self._tracking_statuses.get(descriptor.source_id) == tracking_status:
            return
        self._context_sources.mark_pending_observation(
            descriptor.source_id, None, reconcile=True,
        )
        self._tracking_statuses[descriptor.source_id] = tracking_status

    def _handle_event(self, observation: ResourceObservation) -> int:
        """事件回调路径：只查表并标记 pending，不做 I/O；返回标记的来源数量。"""
        marked = 0
        if observation.gap:
            # 丢失的通知可能属于任一绑定文件，即使本次投递的是目录通知。
            marked = len(self.mark_all_sources())
        source_id = (
            self._source_ids_by_uri.get(observation.uri)
            if observation.uri is not None else None
        )
        if source_id is None:
            logger.debug(
                "忽略未绑定资源的观察通知: reactor=%s uri=%s",
                self._reactor_id,
                observation.uri,
            )
            return marked
        if observation.gap:
            # gap 表示至少一条通知丢失且无法归属到单一来源：必须对全部已绑定
            # 来源重新对账，否则未收到通知的来源会永久静默陈旧。
            logger.warning(
                "资源观察通知出现 gap，已标记全部绑定来源待观察: "
                "reactor=%s uri=%s marked=%s",
                self._reactor_id,
                observation.uri,
                marked,
            )
        revision = observation.revision
        if revision is None:
            if not observation.available:
                raise RuntimeError(
                    "资源来源不可用且没有可用 revision: "
                    f"reactor={self._reactor_id} uri={observation.uri}"
                )
            return marked
        if self._context_sources.mark_pending_observation(source_id, revision):
            marked += 1
        return marked

    def mark_all_sources(self) -> tuple[str, ...]:
        """把全部已绑定来源标记为「需要按权威快照重新对账」。

        用于 gap 后的全量 reconcile：只登记来源 identity，不读取正文、不做 I/O；
        不要求来源已有已知 revision，消费时才从权威快照解析 revision。
        """
        self._require_active()
        return self._context_sources.mark_all_pending_observations(
            tuple(set(self._source_ids_by_uri.values()))
        )

    async def _release_resources(self) -> None:
        """幂等释放：清空绑定并解除订阅；释放后事件不再到达 CSM。"""
        self._released = True
        subscription = self._subscription
        self._source_ids_by_uri.clear()
        self._tracking_statuses.clear()
        if subscription is not None:
            self._sources.unsubscribe_observation(subscription)
            self._subscription = None

    def _require_active(self) -> None:
        if self._released:
            raise RuntimeError(
                "ContextSourceReactor 已释放，不能继续绑定或消费来源: "
                f"reactor={self._reactor_id}"
            )


__all__ = [
    "ContextSourceReactor",
    "PublishedSourceObservationRef",
    "ResourceObservationSourcePort",
    "ResourceSnapshotLike",
]
