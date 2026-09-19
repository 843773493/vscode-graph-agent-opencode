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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from app.core.lifecycle import LifetimeHandle, LifetimeScope
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservation,
    ResourceObservationSubscription,
)
from app.services.infrastructure.resource_platform.sources.observed_source import (
    SourceReconciler,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
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
class SourceObservation:
    """一次待消费的来源观察：只有 identity、URI 与 revision，没有正文。"""

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
        reconciler: SourceReconciler | None = None,
    ) -> None:
        if not isinstance(reactor_id, str) or not reactor_id.strip():
            raise ValueError("ContextSourceReactor.reactor_id 必须是非空字符串")
        self._sources = sources
        self._context_sources = context_sources
        # OpenSpec 3.3:消费方可经 reactor 向 Reconciler 回执 committed
        # revision,registry 侧据此区分 observed/pending/committed。
        self._reconciler = reconciler
        self._lifetime_scope = lifetime_scope
        self._reactor_id = reactor_id
        self._subscription: ResourceObservationSubscription | None = None
        self._release_handle: LifetimeHandle | None = None
        self._source_ids_by_uri: dict[str, str] = {}
        self._observed_tokens: dict[str, tuple[str, str | None]] = {}
        self._released = False
        self._release_registered = False

    @property
    def reactor_id(self) -> str:
        return self._reactor_id

    @property
    def released(self) -> bool:
        return self._released

    @property
    def subscribed_uri_count(self) -> int:
        return len(self._source_ids_by_uri)

    def sync_sources(self) -> tuple[str, ...]:
        """按 CSM 当前注册表增量接线，返回本次新绑定的虚拟 URI。

        该方法只做内存操作：为带 ``resource_uri`` 的 descriptor 建立订阅，
        并把「需要首次观察」的来源标记为待观察。
        """
        self._require_active()
        newly_bound: list[str] = []
        for descriptor in self._context_sources.descriptors():
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

    def drain(self) -> Iterator[SourceObservation]:
        """把已排队的轻量通知转换为待观察来源，不读取任何来源内容。

        已释放的 reactor 返回空迭代器：订阅已经解除，事件不再到达 CSM；
        仍在执行的旧 agent 只保留它已有的上下文，不因为订阅释放而失败。
        """
        if self._released:
            return
        self.ingest_notifications()
        while True:
            pending = self._context_sources.next_pending_observation()
            if pending is None:
                return
            descriptor = self._descriptor_by_source_id(pending.source_id)
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
            yield SourceObservation(
                source_id=pending.source_id,
                resource_uri=resource_uri,
                revision=revision,
            )

    def content_for(self, observation: SourceObservation) -> str:
        """从来源 owner 的权威内存快照读取正文；不触发任何磁盘 I/O。"""
        snapshot = self._sources.snapshot(observation.resource_uri)
        if not snapshot.available:
            raise RuntimeError(
                "来源快照不可用: "
                f"uri={observation.resource_uri} "
                f"revision={observation.revision}"
            )
        return snapshot.content

    def mark_source_committed(self, source_id: str, revision: str) -> None:
        """把「该 revision 已进入已提交上下文」回执给 Reconciler。

        多个未提交变化由 registry 侧合并为 committed→observed 一个待消费
        事实;rewind 感知的 latest-visible-committed 收敛属于 CSM owner 层。
        """
        if self._reconciler is None:
            raise RuntimeError(
                "reactor 未接入 SourceReconciler,无法回执 committed revision: "
                f"reactor_id={self._reactor_id}",
            )
        self._require_active()
        self._reconciler.mark_committed(source_id, revision)

    def source_revision_states(
        self, source_id: str
    ) -> tuple[str, str | None, str | None]:
        """透传 Reconciler 的 (observed, pending, committed) 诊断状态。"""
        if self._reconciler is None:
            raise RuntimeError(
                "reactor 未接入 SourceReconciler,无法查询 revision 状态: "
                f"reactor_id={self._reactor_id}",
            )
        return self._reconciler.revision_states(source_id)

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
            self._register_release()
        self._source_ids_by_uri[resource_uri] = descriptor.source_id
        logger.debug(
            "ContextSourceReactor 绑定来源: reactor=%s source_id=%s uri=%s",
            self._reactor_id,
            descriptor.source_id,
            resource_uri,
        )

    def _seed_if_needed(self, descriptor: ContextSourceDescriptor) -> None:
        """首次观察或 tracking 状态变化时，标记来源需要一次权威快照读取。

        已 tracked 且已知最新 revision 的来源不需要再次读取快照：skill_load
        的显式 ``observe`` 或上一次 observation 已经把同一 revision 送进 CSM。
        """
        resource_uri = descriptor.resource_uri
        if resource_uri is None:
            return
        tracking_status, latest_revision = (
            self._context_sources.source_observation_state(descriptor.source_id)
        )
        token = (tracking_status, latest_revision)
        if self._observed_tokens.get(descriptor.source_id) == token:
            return
        if latest_revision is None:
            snapshot = self._sources.snapshot(resource_uri)
            if snapshot.revision is None:
                self._observed_tokens[descriptor.source_id] = token
                return
            self._context_sources.mark_pending_observation(
                descriptor.source_id,
                snapshot.revision,
            )
        self._observed_tokens[descriptor.source_id] = (
            self._context_sources.source_observation_state(descriptor.source_id)
        )

    def _handle_event(self, observation: ResourceObservation) -> int:
        """事件回调路径：只查表并标记 pending，不做 I/O；返回标记的来源数量。"""
        source_id = self._source_ids_by_uri.get(observation.uri)
        if source_id is None:
            logger.debug(
                "忽略未绑定资源的观察通知: reactor=%s uri=%s",
                self._reactor_id,
                observation.uri,
            )
            return 0
        marked = 0
        if observation.gap:
            # gap 表示至少一条通知丢失且无法归属到单一来源：必须对全部已绑定
            # 来源重新对账，否则未收到通知的来源会永久静默陈旧。
            marked = len(self.mark_all_sources())
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

    def _descriptor_by_source_id(self, source_id: str) -> ContextSourceDescriptor:
        for descriptor in self._context_sources.descriptors():
            if descriptor.source_id == source_id:
                return descriptor
        raise KeyError(
            "待观察来源尚未由 owner 重新注册 descriptor: "
            f"source_id={source_id}"
        )

    def _register_release(self) -> None:
        if self._release_registered:
            return
        # OpenSpec 3.9：登记返回可撤销句柄。reactor 的 close() 与 scope 关闭
        # 都会驱动 _release_resources（幂等），句柄保留给 owner 需要提前撤销时
        # 显式调用 handle.revoke() 使用。
        self._release_handle = self._lifetime_scope.register(
            self._release_resources,
            label=f"context-source-reactor:{self._reactor_id}",
        )
        self._release_registered = True

    async def _release_resources(self) -> None:
        """幂等释放：清空绑定并解除订阅；释放后事件不再到达 CSM。"""
        self._released = True
        subscription = self._subscription
        self._subscription = None
        self._source_ids_by_uri.clear()
        self._observed_tokens.clear()
        if subscription is not None:
            self._sources.unsubscribe_observation(subscription)

    def _require_active(self) -> None:
        if self._released:
            raise RuntimeError(
                "ContextSourceReactor 已释放，不能继续绑定或消费来源: "
                f"reactor={self._reactor_id}"
            )


ReactorCreatedCallback = Callable[[tuple[object, ...], "ContextSourceReactor"], None]


class ContextSourceReactionRegistry:
    """按 agent 缓存身份持有 reactor，并在淘汰或关闭时释放订阅。"""

    def __init__(self, *, lifetime_scope: LifetimeScope) -> None:
        self._lifetime_scope = lifetime_scope
        self._reactors: dict[tuple[object, ...], ContextSourceReactor] = {}

    @property
    def active_keys(self) -> tuple[tuple[object, ...], ...]:
        return tuple(self._reactors)

    def record(
        self,
        owner_key: tuple[object, ...],
        reactor: ContextSourceReactor,
    ) -> None:
        if owner_key in self._reactors:
            raise RuntimeError(
                "ContextSourceReactionRegistry owner key 重复: "
                f"owner_key={owner_key!r}"
            )
        self._reactors[owner_key] = reactor

    async def release(self, owner_key: tuple[object, ...]) -> None:
        """释放指定 owner 的 reactor；未登记时是无操作。"""
        reactor = self._reactors.pop(owner_key, None)
        if reactor is None:
            return
        await reactor.close()

    async def close(self) -> None:
        """释放全部 reactor，并关闭持有它们的 scope。

        逐个释放，最后统一汇总错误：单个 reactor 失败不得阻止其它订阅释放，
        也不得把失败转换成成功。
        """
        errors: list[Exception] = []
        for owner_key in tuple(self._reactors):
            try:
                await self.release(owner_key)
            except Exception as error:  # noqa: BLE001 - 释放错误必须汇总上报
                errors.append(error)
        try:
            await self._lifetime_scope.close()
        except Exception as error:  # noqa: BLE001 - scope 释放错误必须汇总上报
            errors.append(error)
        if errors:
            raise ExceptionGroup("ContextSourceReactionRegistry close 失败", errors)


__all__ = [
    "ContextSourceReactionRegistry",
    "ContextSourceReactor",
    "ReactorCreatedCallback",
    "ResourceObservationSourcePort",
    "ResourceSnapshotLike",
    "SourceObservation",
]