"""资源观察侧的轻量通知通道（EventChannelService 之上的 typed adapter）。

本模块只传递「某来源有新 revision」这一内存事实。通知刻意不携带正文、
宿主机路径或 credential；consumer 收到通知后必须回到来源 owner 发布的
权威内存快照，不能把通知本身当作 durable 事实。

OpenSpec 3.8-A：订阅者队列、gap 溢出与订阅释放语义由通用
EventChannelService 的 ``resource.observe/*`` channel 承载；本模块只保留
:class:`ResourceObservation` 值对象、轻量校验和类型化外观，公开 API 与既有
调用方（registry、reactor）完全兼容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from typing import Final

from app.core.identifier import create_prefixed_id
from app.services.infrastructure.events.event_channel_service import (
    RESOURCE_OBSERVE_CHANNEL_KIND,
    ChannelDelivery,
    EventChannelService,
    EventChannelSpec,
    EventChannelSubscription,
    channel_name,
)

# 既有模块常量保持不变：当前 adapter 把全部 provider instance 复用进同一条
# 默认 channel；出现第二个 provider instance 时再拆分为按 instance 命名的
# channel（值对象与合同不变）。
RESOURCE_OBSERVATION_CHANNEL: Final[str] = channel_name(
    RESOURCE_OBSERVE_CHANNEL_KIND, "*"
)
RESOURCE_OBSERVATION_QUEUE_SIZE: Final[int] = 64

# 事件只允许携带这些轻量字段：来源虚拟 identity、内容 revision、
# 可用性，以及「通知是否因溢出丢失」的 gap 标记。新增字段必须仍然满足
# 「不携带正文/credential/宿主机路径」的红线。
ALLOWED_NOTIFICATION_FIELDS: Final[frozenset[str]] = frozenset(
    {"uri", "revision", "available", "gap"}
)


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    """一次「来源有新 revision」的内存通知，不含正文。"""

    uri: str
    revision: str | None
    available: bool
    gap: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri:
            raise ValueError("ResourceObservation.uri 必须是非空字符串")
        if self.revision is not None and (
            not isinstance(self.revision, str) or not self.revision
        ):
            raise ValueError("ResourceObservation.revision 必须是非空字符串或 None")


@dataclass(frozen=True, slots=True)
class ResourceObservationDelivery:
    """一次投递结果：订阅者 id 与是否发生了 gap。"""

    subscription_id: str
    gap: bool


def _observation_from_delivery(
    delivery: ChannelDelivery[ResourceObservation],
) -> ResourceObservation:
    """把通用投递信封映射回 ResourceObservation；gap 标记来自投递信封。"""
    observation = delivery.event
    if delivery.gap and not observation.gap:
        return replace(observation, gap=True)
    return observation


class ResourceObservationSubscription:
    """单个订阅者对 `resource.observe/*` 的只读消费句柄。

    订阅由持有它的 owner 释放；释放后队列里不再出现新的通知，但已经排队的
    通知仍可被读取，避免 owner 在关闭过程中丢失已观察到的事实。队列与 gap
    状态由 EventChannelService 的订阅句柄承载，这里只做类型化映射。
    """

    def __init__(
        self,
        *,
        subscription_id: str,
        label: str,
        handle: EventChannelSubscription[ResourceObservation],
    ) -> None:
        self.subscription_id = subscription_id
        self.label = label
        self._handle = handle

    @property
    def released(self) -> bool:
        return self._handle.released

    async def next(self) -> ResourceObservation:
        """等待下一条通知；不在这里做任何来源读取或业务判断。"""
        return _observation_from_delivery(await self._handle.next())

    def pending(self) -> tuple[ResourceObservation, ...]:
        """排空当前已排队的通知，供无 await 边界的同步消费点使用。"""
        return tuple(
            _observation_from_delivery(delivery)
            for delivery in self._handle.pending()
        )

    def mark_released(self) -> None:
        self._handle.mark_released()


class ResourceObservationChannel:
    """`resource.observe/*` 的轻量内存通道。

    OpenSpec 3.8-A：本类是 :class:`EventChannelService` 之上的 typed adapter，
    队列、gap 与订阅释放语义由通用 channel 承载。通道只负责按订阅者分发
    通知，不实现 durable 历史、cursor 重放或跨进程投递。
    """

    def __init__(
        self,
        *,
        max_queue_size: int = RESOURCE_OBSERVATION_QUEUE_SIZE,
        event_service: EventChannelService | None = None,
    ) -> None:
        if max_queue_size <= 0:
            raise ValueError("ResourceObservationChannel.max_queue_size 必须大于 0")
        self._event_service = event_service or EventChannelService()
        self._channel = self._event_service.ensure_channel(
            EventChannelSpec(
                name=RESOURCE_OBSERVATION_CHANNEL,
                overflow_policy="gap",
                max_queue_size=max_queue_size,
                history_size=0,
            )
        )

    @property
    def channel(self) -> str:
        return RESOURCE_OBSERVATION_CHANNEL

    @property
    def event_service(self) -> EventChannelService:
        """暴露所属的 channel 服务，供组合根共享同一条事件基础设施。"""
        return self._event_service

    @property
    def subscriber_ids(self) -> tuple[str, ...]:
        return self._channel.subscriber_ids

    def subscribe(self, *, label: str) -> ResourceObservationSubscription:
        if not isinstance(label, str) or not label.strip():
            raise ValueError("ResourceObservationChannel.subscribe 需要非空 label")
        handle = self._channel.subscribe(
            label=label,
            subscription_id=create_prefixed_id("robs"),
        )
        return ResourceObservationSubscription(
            subscription_id=handle.subscription_id,
            label=label,
            handle=handle,
        )

    def unsubscribe(self, subscription: ResourceObservationSubscription) -> bool:
        removed = self._channel.unsubscribe(subscription.subscription_id)
        subscription.mark_released()
        return removed

    def notify(
        self,
        observation: ResourceObservation,
    ) -> tuple[ResourceObservationDelivery, ...]:
        """把通知同步投递给全部订阅者，返回每个订阅者的投递结果。

        这是同步方法，便于在共享 watcher 的事件循环任务里直接调用；它不做
        I/O，也不阻塞等待 consumer。某个订阅者的队列满时只丢弃该订阅者最旧的
        一条通知并投递 gap 标记，不影响其它订阅者或该订阅者队列里的其它来源；
        gap 被 consumer 取走后恢复投递（语义由通用 channel 的 gap 策略承载）。
        """
        assert_notification_is_lightweight(observation)
        receipts = self._channel.publish(observation)
        return tuple(
            ResourceObservationDelivery(
                subscription_id=receipt.subscription_id,
                gap=receipt.overflow,
            )
            for receipt in receipts
        )


def assert_notification_is_lightweight(observation: ResourceObservation) -> None:
    """类级 + 值级校验：通知既不能有额外字段，也不能夹带重内容。

    字段集合取自 dataclass 定义（不是实例 __slots__），因此新增字段、改名或
    把正文塞进已有字段都会在这里显式失败，而不是恒真通过。
    """
    declared = {field.name for field in fields(ResourceObservation)}
    unexpected = declared - ALLOWED_NOTIFICATION_FIELDS
    if unexpected:
        raise RuntimeError(
            "ResourceObservation 声明了未允许的字段: " + ",".join(sorted(unexpected))
        )
    if not isinstance(observation.uri, str) or not observation.uri.startswith(
        "boxteam://"
    ):
        raise RuntimeError(
            f"资源通知 uri 必须是 boxteam:// 虚拟 identity: {observation.uri!r}"
        )
    # uri 是虚拟 URI（允许 '/'），但必须有界且不含相对路径形状：
    # 超长值只可能是把正文塞进 uri，'..' 是宿主机相对路径的特征。
    if len(observation.uri) > 512:
        raise RuntimeError(
            f"资源通知 uri 超过长度上限 512: 实际 {len(observation.uri)}"
        )
    if ".." in observation.uri:
        raise RuntimeError(
            f"资源通知 uri 不允许包含 '..' 相对路径形状: {observation.uri!r}"
        )
    if observation.revision is not None and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", observation.revision
    ):
        raise RuntimeError(
            "资源通知 revision 必须是完整 sha256 摘要"
            f"（sha256: + 64 位小写 hex）: 长度 {len(observation.revision)}"
        )
    if not isinstance(observation.available, bool) or not isinstance(
        observation.gap, bool
    ):
        raise TypeError("资源通知 available/gap 必须是布尔值")


__all__ = [
    "ALLOWED_NOTIFICATION_FIELDS",
    "RESOURCE_OBSERVATION_CHANNEL",
    "RESOURCE_OBSERVATION_QUEUE_SIZE",
    "ResourceObservation",
    "ResourceObservationChannel",
    "ResourceObservationDelivery",
    "ResourceObservationSubscription",
    "assert_notification_is_lightweight",
]
