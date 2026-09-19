"""通用 EventChannelService：按 channel 隔离的进程内轻量通知传输。

OpenSpec 3.8 的核心抽象。channel 按 ``kind/{参数}`` 命名，至少支持：

- ``job.events/{job_id}``
- ``resource.observe/{provider_instance}``
- ``resource.state/{owner_domain}``
- ``config.lifecycle/{domain}``
- ``context.source/{workspace_id}``
- ``mcp.catalog/workspace``（MCP 工具目录）

每条 channel 拥有独立的订阅者注册表、有界队列、单调递增 event sequence、
可选短期历史与 cursor 重放；一条 channel 的订阅者溢出或队列积压不影响其它
channel（故障域隔离）。

溢出策略可插拔：

- ``gap``：丢最旧一条 + gap 标记 + 消费后恢复投递（语义与
  ``ResourceObservationChannel`` 完全一致）；
- ``fail_closed``：订阅者队列满即标记溢出错误并从后续投递移除（语义与
  ``JobEventBus`` 一致）。

**这是短期通知传输，不是 durable 事实库**：历史环形缓冲被覆盖后无法重放
更早的事件，也不替代 Session 事件提交；业务 durable state 仍由各 domain
owner 保存。本模块不承载业务决策，也不提供可安装的 provider/loader。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Generic, Literal, Protocol, TypeAlias, TypeVar

from app.core.identifier import create_prefixed_id

EventT = TypeVar("EventT")

JOB_EVENTS_CHANNEL_KIND: Final[str] = "job.events"
RESOURCE_OBSERVE_CHANNEL_KIND: Final[str] = "resource.observe"
RESOURCE_STATE_CHANNEL_KIND: Final[str] = "resource.state"
CONFIG_LIFECYCLE_CHANNEL_KIND: Final[str] = "config.lifecycle"
CONTEXT_SOURCE_CHANNEL_KIND: Final[str] = "context.source"
MCP_CATALOG_CHANNEL_KIND: Final[str] = "mcp.catalog"

# channel kind 是闭集：新增 kind 属于代码变更（同步更新本清单与 AGENTS.md），
# 不提供运行时可配置的扩展点。
CHANNEL_KINDS: Final[frozenset[str]] = frozenset(
    {
        JOB_EVENTS_CHANNEL_KIND,
        RESOURCE_OBSERVE_CHANNEL_KIND,
        RESOURCE_STATE_CHANNEL_KIND,
        CONFIG_LIFECYCLE_CHANNEL_KIND,
        CONTEXT_SOURCE_CHANNEL_KIND,
        MCP_CATALOG_CHANNEL_KIND,
    }
)

OverflowPolicyName: TypeAlias = Literal["gap", "fail_closed"]

DEFAULT_CHANNEL_QUEUE_SIZE: Final[int] = 64
DEFAULT_REPLAY_LIMIT: Final[int] = 100


def channel_name(kind: str, parameter: str) -> str:
    """构造并校验 ``kind/{参数}`` 形式的 channel 名。"""
    if not isinstance(kind, str) or kind not in CHANNEL_KINDS:
        raise ValueError(
            f"未知 channel kind: {kind!r}；允许的 kind: {sorted(CHANNEL_KINDS)}"
        )
    if not isinstance(parameter, str) or not parameter:
        raise ValueError("channel 参数必须是非空字符串")
    if parameter.strip() != parameter:
        raise ValueError(f"channel 参数不允许前导/尾随空白: {parameter!r}")
    if "/" in parameter:
        raise ValueError(f"channel 参数不允许包含 '/': {parameter!r}")
    return f"{kind}/{parameter}"


def parse_channel_name(name: str) -> tuple[str, str]:
    """解析 channel 名，返回 ``(kind, parameter)``；非法形式显式报错。"""
    if not isinstance(name, str) or not name:
        raise ValueError("channel 名必须是非空字符串")
    kind, separator, parameter = name.partition("/")
    if not separator or not parameter:
        raise ValueError(f"channel 名必须是 'kind/参数' 形式: {name!r}")
    if kind not in CHANNEL_KINDS:
        raise ValueError(
            f"未知 channel kind: {kind!r}；允许的 kind: {sorted(CHANNEL_KINDS)}"
        )
    if "/" in parameter:
        raise ValueError(f"channel 参数不允许包含 '/': {name!r}")
    return kind, parameter


class EventChannelError(RuntimeError):
    """EventChannelService 的显式错误基类。"""


class EventChannelSpecConflictError(EventChannelError):
    """同一名字的 channel 已存在但 spec 不一致。"""


class EventChannelOverflowError(EventChannelError):
    """fail_closed 策略下订阅者队列溢出，订阅已被移除。"""


class EventChannelHistoryDisabledError(EventChannelError):
    """对未启用短期历史的 channel 请求 cursor 重放。"""


@dataclass(frozen=True, slots=True)
class ChannelDelivery(Generic[EventT]):
    """一条已入队的投递：channel 级单调 sequence、channel 盖章时间与事件值。

    ``gap=True`` 表示该投递是 gap 标记：此前至少有一条通知因溢出丢失，且无法
    归属到具体事件；consumer 收到后必须按自己的权威状态重新对账。
    """

    sequence: int
    occurred_at: datetime
    gap: bool
    event: EventT


@dataclass(frozen=True, slots=True)
class ChannelPublishReceipt:
    """一次 publish 对单个订阅者的投递结果。"""

    subscription_id: str
    # 本次是否实际向该订阅者投递了新内容（gap 抑制期间为 False）。
    delivered: bool
    # 本次投递是否发生溢出：gap 策略=投递/维持 gap 标记；fail_closed=订阅者被移除。
    overflow: bool


class ChannelEventSink(Protocol[EventT]):
    """外部队列订阅者的窄接口（fail_closed 接入）。

    ``offer`` 返回 False 表示该订阅者溢出；channel 会把它从后续投递中移除。
    溢出错误的具体形态（错误类型、日志）由 sink 的 owner 决定。
    """

    def offer(self, event: EventT, *, sequence: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class EventChannelSpec:
    """channel 的创建参数；同名 channel 的 spec 冲突必须显式报错。"""

    name: str
    overflow_policy: OverflowPolicyName = "gap"
    max_queue_size: int = DEFAULT_CHANNEL_QUEUE_SIZE
    # 0 表示不保留短期历史（纯实时通知，如 resource.observe）。
    history_size: int = 0

    def __post_init__(self) -> None:
        parse_channel_name(self.name)
        if self.overflow_policy not in ("gap", "fail_closed"):
            raise ValueError(
                "EventChannelSpec.overflow_policy 只能是 'gap' 或 'fail_closed': "
                f"{self.overflow_policy!r}"
            )
        if not isinstance(self.max_queue_size, int) or self.max_queue_size <= 0:
            raise ValueError("EventChannelSpec.max_queue_size 必须是正整数")
        if not isinstance(self.history_size, int) or self.history_size < 0:
            raise ValueError("EventChannelSpec.history_size 必须是非负整数")


class _ChannelSubscriber(Generic[EventT]):
    """channel 内部的订阅者状态；故障域按 channel 隔离，订阅者之间按队列隔离。"""

    __slots__ = (
        "gap_queued",
        "label",
        "overflow_error",
        "queue",
        "sink",
        "subscription_id",
    )

    def __init__(
        self,
        *,
        subscription_id: str,
        label: str,
        queue: asyncio.Queue[ChannelDelivery[EventT]] | None,
        sink: ChannelEventSink[EventT] | None,
    ) -> None:
        self.subscription_id = subscription_id
        self.label = label
        self.queue = queue
        self.sink = sink
        # gap 策略：队列里存在未消费的 gap 投递时为 True；被消费后必须复位，
        # 否则该订阅者会在首次溢出后永久静默。
        self.gap_queued = False
        # fail_closed 策略：溢出时记录的错误；消费句柄此后必须显式抛出。
        self.overflow_error: EventChannelOverflowError | None = None


class EventChannelSubscription(Generic[EventT]):
    """单个订阅者对一条 channel 的只读消费句柄。

    订阅由持有它的 owner 释放；释放后队列里不再出现新的投递，但已经排队的
    投递仍可被读取，避免 owner 在关闭过程中丢失已观察到的事实。
    """

    def __init__(
        self,
        *,
        subscription_id: str,
        label: str,
        channel_name: str,
        subscriber: _ChannelSubscriber[EventT],
    ) -> None:
        self.subscription_id = subscription_id
        self.label = label
        self.channel_name = channel_name
        self._subscriber = subscriber
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def mark_released(self) -> None:
        self._released = True

    async def next(self) -> ChannelDelivery[EventT]:
        """等待下一条投递；溢出错误优先于剩余排队投递。"""
        error = self._subscriber.overflow_error
        if error is not None:
            raise error
        assert self._subscriber.queue is not None
        delivery = await self._subscriber.queue.get()
        self._after_consume(delivery)
        return delivery

    def pending(self) -> tuple[ChannelDelivery[EventT], ...]:
        """排空当前已排队的投递，供无 await 边界的同步消费点使用。"""
        error = self._subscriber.overflow_error
        if error is not None:
            raise error
        assert self._subscriber.queue is not None
        queue = self._subscriber.queue
        items: list[ChannelDelivery[EventT]] = []
        while not queue.empty():
            delivery = queue.get_nowait()
            self._after_consume(delivery)
            items.append(delivery)
        return tuple(items)

    def _after_consume(self, delivery: ChannelDelivery[EventT]) -> None:
        """gap 投递一被取走就恢复后续投递；对账范围由 consumer 决定。"""
        if delivery.gap:
            self._subscriber.gap_queued = False


class EventChannel(Generic[EventT]):
    """一条按 ``kind/{参数}`` 命名的通知 channel。

    这是短期通知传输：按订阅者隔离有界队列、单调递增 event sequence、可选
    短期历史与 cursor 重放。它不是 durable 事实库，不替代 Session 事件提交；
    历史环形缓冲被覆盖后无法重放更早的事件。
    """

    def __init__(self, *, spec: EventChannelSpec) -> None:
        self._spec = spec
        self._subscribers: dict[str, _ChannelSubscriber[EventT]] = {}
        self._history: deque[ChannelDelivery[EventT]] | None = (
            deque(maxlen=spec.history_size) if spec.history_size > 0 else None
        )
        self._last_sequence = 0

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def spec(self) -> EventChannelSpec:
        return self._spec

    @property
    def overflow_policy(self) -> OverflowPolicyName:
        return self._spec.overflow_policy

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def subscriber_ids(self) -> tuple[str, ...]:
        return tuple(self._subscribers)

    @property
    def history(self) -> tuple[EventT, ...]:
        """短期历史中的事件快照（不含 gap 投递）；未启用历史时为空元组。"""
        if self._history is None:
            return ()
        return tuple(delivery.event for delivery in self._history)

    def subscribe(
        self,
        *,
        label: str,
        subscription_id: str | None = None,
        max_queue_size: int | None = None,
    ) -> EventChannelSubscription[EventT]:
        """登记一个由 channel 持有队列的订阅者，返回消费句柄。"""
        if not isinstance(label, str) or not label.strip():
            raise ValueError("EventChannel.subscribe 需要非空 label")
        if max_queue_size is not None and (
            not isinstance(max_queue_size, int) or max_queue_size <= 0
        ):
            raise ValueError("EventChannel.subscribe max_queue_size 必须大于 0")
        queue_size = max_queue_size or self._spec.max_queue_size
        resolved_id = subscription_id or create_prefixed_id("chan")
        if resolved_id in self._subscribers:
            raise ValueError(f"channel 订阅 id 重复: {resolved_id}")
        subscriber: _ChannelSubscriber[EventT] = _ChannelSubscriber(
            subscription_id=resolved_id,
            label=label,
            queue=asyncio.Queue(maxsize=queue_size),
            sink=None,
        )
        self._subscribers[resolved_id] = subscriber
        return EventChannelSubscription(
            subscription_id=resolved_id,
            label=label,
            channel_name=self._spec.name,
            subscriber=subscriber,
        )

    def subscribe_with_sink(
        self,
        sink: ChannelEventSink[EventT],
        *,
        label: str,
        subscription_id: str | None = None,
    ) -> str:
        """登记一个自带队列的外部订阅者（fail_closed 接入），返回订阅 id。

        sink 订阅者只支持 fail_closed：``offer`` 返回 False 即被移除。gap
        语义只能由 channel 内部队列承载，不允许混入外部 sink。
        """
        if self._spec.overflow_policy != "fail_closed":
            raise ValueError(
                "subscribe_with_sink 只支持 fail_closed channel: "
                f"channel={self._spec.name} policy={self._spec.overflow_policy}"
            )
        if not isinstance(label, str) or not label.strip():
            raise ValueError("EventChannel.subscribe_with_sink 需要非空 label")
        offer = getattr(sink, "offer", None)
        if not callable(offer):
            raise TypeError(
                "sink 必须实现 offer(event, *, sequence) -> bool: "
                f"{type(sink).__name__}"
            )
        resolved_id = subscription_id or create_prefixed_id("chan")
        if resolved_id in self._subscribers:
            raise ValueError(f"channel 订阅 id 重复: {resolved_id}")
        self._subscribers[resolved_id] = _ChannelSubscriber(
            subscription_id=resolved_id,
            label=label,
            queue=None,
            sink=sink,
        )
        return resolved_id

    def unsubscribe(self, subscription: EventChannelSubscription[EventT] | str) -> bool:
        """解除订阅；已排队的投递仍可被该句柄读取，但不再有新投递。"""
        if isinstance(subscription, EventChannelSubscription):
            subscription_id = subscription.subscription_id
        elif isinstance(subscription, str):
            subscription_id = subscription
        else:
            raise TypeError(
                "EventChannel.unsubscribe 需要 EventChannelSubscription 或订阅 id 字符串: "
                f"{type(subscription).__name__}"
            )
        removed = self._subscribers.pop(subscription_id, None) is not None
        if isinstance(subscription, EventChannelSubscription):
            subscription.mark_released()
        return removed

    def publish(self, event: EventT) -> tuple[ChannelPublishReceipt, ...]:
        """把一条事件同步投递给全部订阅者，返回每个订阅者的投递结果。

        同步方法：不做 I/O、不等待 consumer，可在共享 watcher 的事件循环任务
        里直接调用。所有订阅者共享同一个 channel 级单调 sequence；短期历史
        （若启用）先于订阅者投递写入，保证 cursor 重放与实时投递同一顺序。
        """
        if event is None:
            raise ValueError("EventChannel.publish event 不能为 None")
        self._last_sequence += 1
        sequence = self._last_sequence
        occurred_at = datetime.now(UTC)
        if self._history is not None:
            self._history.append(
                ChannelDelivery(
                    sequence=sequence,
                    occurred_at=occurred_at,
                    gap=False,
                    event=event,
                )
            )
        receipts: list[ChannelPublishReceipt] = []
        removals: list[str] = []
        for subscription_id, subscriber in tuple(self._subscribers.items()):
            if subscriber.sink is not None:
                accepted = bool(subscriber.sink.offer(event, sequence=sequence))
                if accepted:
                    receipts.append(
                        ChannelPublishReceipt(
                            subscription_id=subscription_id,
                            delivered=True,
                            overflow=False,
                        )
                    )
                else:
                    removals.append(subscription_id)
                    receipts.append(
                        ChannelPublishReceipt(
                            subscription_id=subscription_id,
                            delivered=False,
                            overflow=True,
                        )
                    )
            elif self._spec.overflow_policy == "gap":
                receipts.append(self._offer_gap(subscriber, sequence, occurred_at, event))
            else:
                receipt, overflowed = self._offer_fail_closed(
                    subscriber, sequence, occurred_at, event
                )
                receipts.append(receipt)
                if overflowed:
                    removals.append(subscription_id)
        for subscription_id in removals:
            self._subscribers.pop(subscription_id, None)
        return tuple(receipts)

    def replay(
        self,
        *,
        after_sequence: int | None = None,
        limit: int = DEFAULT_REPLAY_LIMIT,
    ) -> tuple[ChannelDelivery[EventT], ...]:
        """按 cursor（sequence）重放短期历史；只对启用历史的 channel 可用。

        这是短期通知传输的重放辅助，不是 durable 事实库：环形缓冲被覆盖后
        无法重放更早的事件，也不替代 Session 事件提交。
        """
        if self._history is None:
            raise EventChannelHistoryDisabledError(
                f"channel 未启用短期历史，无法 cursor 重放: channel={self._spec.name}"
            )
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("EventChannel.replay limit 必须是正整数")
        if after_sequence is not None and (
            not isinstance(after_sequence, int) or after_sequence < 0
        ):
            raise ValueError("EventChannel.replay after_sequence 必须是非负整数或 None")
        deliveries = tuple(self._history)
        if after_sequence is not None:
            deliveries = tuple(
                delivery for delivery in deliveries if delivery.sequence > after_sequence
            )
        return tuple(deliveries[-limit:])

    def _offer_gap(
        self,
        subscriber: _ChannelSubscriber[EventT],
        sequence: int,
        occurred_at: datetime,
        event: EventT,
    ) -> ChannelPublishReceipt:
        """gap 策略投递：丢最旧一条 + gap 标记 + 消费后恢复投递。

        gap 标记只表示「有通知丢失」，不代表丢失的具体事件；consumer 收到后
        必须按权威状态重新对账。队列里已有未消费的 gap 时不再重复投递，但仍
        显式报告溢出，让投递方保持可见。
        """
        assert subscriber.queue is not None
        if subscriber.gap_queued:
            return ChannelPublishReceipt(
                subscription_id=subscriber.subscription_id,
                delivered=False,
                overflow=True,
            )
        queue = subscriber.queue
        if queue.full():
            # 丢最旧的一条，保留其它事件已排队的投递；随后放入 gap 标记。
            queue.get_nowait()
            queue.put_nowait(
                ChannelDelivery(
                    sequence=sequence,
                    occurred_at=occurred_at,
                    gap=True,
                    event=event,
                )
            )
            subscriber.gap_queued = True
            return ChannelPublishReceipt(
                subscription_id=subscriber.subscription_id,
                delivered=True,
                overflow=True,
            )
        queue.put_nowait(
            ChannelDelivery(
                sequence=sequence,
                occurred_at=occurred_at,
                gap=False,
                event=event,
            )
        )
        return ChannelPublishReceipt(
            subscription_id=subscriber.subscription_id,
            delivered=True,
            overflow=False,
        )

    def _offer_fail_closed(
        self,
        subscriber: _ChannelSubscriber[EventT],
        sequence: int,
        occurred_at: datetime,
        event: EventT,
    ) -> tuple[ChannelPublishReceipt, bool]:
        """fail_closed 策略投递：队列满即记录溢出错误并从后续投递移除。"""
        assert subscriber.queue is not None
        delivery = ChannelDelivery(
            sequence=sequence,
            occurred_at=occurred_at,
            gap=False,
            event=event,
        )
        try:
            subscriber.queue.put_nowait(delivery)
        except asyncio.QueueFull:
            subscriber.overflow_error = EventChannelOverflowError(
                "事件订阅者队列溢出，订阅已从 channel 移除: "
                f"channel={self._spec.name} "
                f"subscription_id={subscriber.subscription_id} "
                f"label={subscriber.label} max_queue_size={subscriber.queue.maxsize} "
                f"event_sequence={sequence}"
            )
            return (
                ChannelPublishReceipt(
                    subscription_id=subscriber.subscription_id,
                    delivered=False,
                    overflow=True,
                ),
                True,
            )
        return (
            ChannelPublishReceipt(
                subscription_id=subscriber.subscription_id,
                delivered=True,
                overflow=False,
            ),
            False,
        )


class EventChannelService:
    """按 channel 隔离队列、cursor、背压与故障域的进程内事件服务。

    每条 channel 拥有独立的订阅者注册表、队列、sequence 与历史；一条 channel
    的订阅者溢出或队列积压不影响其它 channel。服务只负责通知传输，不承载
    业务决策，也不是 durable 事实库。
    """

    def __init__(self) -> None:
        self._channels: dict[str, EventChannel] = {}

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(self._channels)

    @property
    def channels(self) -> tuple[EventChannel, ...]:
        """当前全部 channel 快照（按创建顺序）。"""
        return tuple(self._channels.values())

    def ensure_channel(self, spec: EventChannelSpec) -> EventChannel:
        """取得或创建 channel；同名 channel 的 spec 冲突必须显式报错。"""
        if not isinstance(spec, EventChannelSpec):
            raise TypeError(
                "EventChannelService.ensure_channel 需要 EventChannelSpec: "
                f"{type(spec).__name__}"
            )
        existing = self._channels.get(spec.name)
        if existing is not None:
            if existing.spec != spec:
                raise EventChannelSpecConflictError(
                    f"channel 已存在且 spec 冲突: channel={spec.name} "
                    f"existing={existing.spec} requested={spec}"
                )
            return existing
        channel = EventChannel(spec=spec)
        self._channels[spec.name] = channel
        return channel

    def channel(self, name: str) -> EventChannel:
        """取得已存在的 channel；不存在或名字非法时显式报错。"""
        parse_channel_name(name)
        found = self._channels.get(name)
        if found is None:
            raise KeyError(f"channel 不存在: {name}")
        return found

    def find_channel(self, name: str) -> EventChannel | None:
        """按名字查找 channel；不存在时返回 None，名字非法时显式报错。"""
        parse_channel_name(name)
        return self._channels.get(name)


__all__ = [
    "CHANNEL_KINDS",
    "CONFIG_LIFECYCLE_CHANNEL_KIND",
    "CONTEXT_SOURCE_CHANNEL_KIND",
    "JOB_EVENTS_CHANNEL_KIND",
    "MCP_CATALOG_CHANNEL_KIND",
    "RESOURCE_OBSERVE_CHANNEL_KIND",
    "RESOURCE_STATE_CHANNEL_KIND",
    "ChannelDelivery",
    "ChannelEventSink",
    "ChannelPublishReceipt",
    "EventChannel",
    "EventChannelError",
    "EventChannelHistoryDisabledError",
    "EventChannelOverflowError",
    "EventChannelService",
    "EventChannelSpec",
    "EventChannelSpecConflictError",
    "EventChannelSubscription",
    "OverflowPolicyName",
    "channel_name",
    "parse_channel_name",
]
