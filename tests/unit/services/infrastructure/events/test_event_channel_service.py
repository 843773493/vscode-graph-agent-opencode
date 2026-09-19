"""通用 EventChannelService 的 channel 隔离、溢出策略与 cursor 重放合同测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.services.infrastructure.events.channel_events import mcp_catalog_channel_name
from app.services.infrastructure.events.event_channel_service import (
    CHANNEL_KINDS,
    CONFIG_LIFECYCLE_CHANNEL_KIND,
    CONTEXT_SOURCE_CHANNEL_KIND,
    JOB_EVENTS_CHANNEL_KIND,
    RESOURCE_OBSERVE_CHANNEL_KIND,
    RESOURCE_STATE_CHANNEL_KIND,
    EventChannelHistoryDisabledError,
    EventChannelOverflowError,
    EventChannelService,
    EventChannelSpec,
    EventChannelSpecConflictError,
    channel_name,
    parse_channel_name,
)


def test_channel_name_helpers_cover_all_six_kinds() -> None:
    """六类 channel 都能按 `kind/{参数}` 构造与解析。"""
    assert channel_name(JOB_EVENTS_CHANNEL_KIND, "job_1") == "job.events/job_1"
    assert (
        channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "provider-1")
        == "resource.observe/provider-1"
    )
    assert channel_name(RESOURCE_STATE_CHANNEL_KIND, "debug") == "resource.state/debug"
    assert (
        channel_name(CONFIG_LIFECYCLE_CHANNEL_KIND, "workspace")
        == "config.lifecycle/workspace"
    )
    assert (
        channel_name(CONTEXT_SOURCE_CHANNEL_KIND, "ws-1") == "context.source/ws-1"
    )
    assert mcp_catalog_channel_name() == "mcp.catalog/workspace"
    assert CHANNEL_KINDS == frozenset(
        {
            "job.events",
            "resource.observe",
            "resource.state",
            "config.lifecycle",
            "context.source",
            "mcp.catalog",
        }
    )
    for name, expected in (
        ("job.events/job_1", ("job.events", "job_1")),
        ("resource.observe/provider-1", ("resource.observe", "provider-1")),
        ("resource.state/debug", ("resource.state", "debug")),
        ("config.lifecycle/workspace", ("config.lifecycle", "workspace")),
        ("context.source/ws-1", ("context.source", "ws-1")),
    ):
        assert parse_channel_name(name) == expected


def test_channel_name_rejects_invalid_kind_and_parameter() -> None:
    with pytest.raises(ValueError, match="未知 channel kind"):
        channel_name("unknown.kind", "param")
    with pytest.raises(ValueError, match="非空字符串"):
        channel_name(JOB_EVENTS_CHANNEL_KIND, "")
    with pytest.raises(ValueError, match="不允许包含 '/'"):
        channel_name(JOB_EVENTS_CHANNEL_KIND, "a/b")
    with pytest.raises(ValueError, match="空白"):
        channel_name(JOB_EVENTS_CHANNEL_KIND, " job ")
    with pytest.raises(ValueError, match="'kind/参数' 形式"):
        parse_channel_name("no-slash")
    with pytest.raises(ValueError, match="未知 channel kind"):
        parse_channel_name("unknown.kind/param")


def test_channels_are_isolated_fault_domains() -> None:
    """一条 channel 的订阅者溢出不得影响其它 channel 的订阅者与 sequence。"""
    service = EventChannelService()
    noisy = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            overflow_policy="gap",
            max_queue_size=1,
        )
    )
    quiet = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(RESOURCE_STATE_CHANNEL_KIND, "debug"),
            overflow_policy="gap",
            max_queue_size=8,
        )
    )
    noisy_sub = noisy.subscribe(label="noisy-consumer")
    quiet_sub = quiet.subscribe(label="quiet-consumer")

    # 让 noisy channel 的订阅者进入 gap 抑制状态。
    noisy.publish("n1")
    receipts = noisy.publish("n2")
    assert [receipt.overflow for receipt in receipts] == [True]

    # quiet channel 不受影响：投递正常、无 gap、sequence 独立计数。
    receipts = quiet.publish("q1")
    assert [receipt.overflow for receipt in receipts] == [False]
    deliveries = quiet_sub.pending()
    assert [(item.sequence, item.gap, item.event) for item in deliveries] == [
        (1, False, "q1")
    ]
    assert noisy.last_sequence == 2
    assert quiet.last_sequence == 1
    assert noisy_sub.pending()[0].gap is True


def test_gap_overflow_drops_oldest_marks_gap_and_recovers() -> None:
    """gap 策略：丢最旧一条 + gap 标记 + 消费后恢复投递（与既有通道语义一致）。"""
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            overflow_policy="gap",
            max_queue_size=1,
        )
    )
    subscription = channel.subscribe(label="slow")

    channel.publish("r1")
    receipts = channel.publish("r2")
    assert [(r.delivered, r.overflow) for r in receipts] == [(True, True)]

    deliveries = subscription.pending()
    assert [(d.gap, d.event) for d in deliveries] == [(True, "r2")]

    # gap 已消费：后续通知必须恢复入队。
    receipts = channel.publish("r3")
    assert [(r.delivered, r.overflow) for r in receipts] == [(True, False)]
    assert [d.event for d in subscription.pending()] == ["r3"]


def test_gap_overflow_keeps_single_gap_marker_while_overflow_persists() -> None:
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            overflow_policy="gap",
            max_queue_size=1,
        )
    )
    subscription = channel.subscribe(label="slow")
    channel.publish("r1")
    # r2 触发溢出：丢最旧一条并投递 gap 标记（delivered=True、overflow=True）。
    receipts = channel.publish("r2")
    assert [(r.delivered, r.overflow) for r in receipts] == [(True, True)]
    # 溢出持续期间抑制后续投递，但每次都显式报告溢出。
    for revision in ("r3", "r4"):
        receipts = channel.publish(revision)
        assert [(r.delivered, r.overflow) for r in receipts] == [(False, True)]
    deliveries = subscription.pending()
    assert len(deliveries) == 1
    assert deliveries[0].gap is True


async def test_fail_closed_overflow_removes_subscriber_and_raises_on_next() -> None:
    """fail_closed 策略：队列满即记录溢出错误并从后续投递移除。"""
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(JOB_EVENTS_CHANNEL_KIND, "job_1"),
            overflow_policy="fail_closed",
            max_queue_size=2,
            history_size=10,
        )
    )
    slow = channel.subscribe(label="slow")
    healthy = channel.subscribe(label="healthy")

    channel.publish("e1")
    channel.publish("e2")
    # healthy 订阅者及时消费，保持队列有空间；slow 订阅者积压。
    healthy.pending()
    receipts = channel.publish("e3")
    by_id = {receipt.subscription_id: receipt for receipt in receipts}
    assert by_id[slow.subscription_id].overflow is True
    assert by_id[slow.subscription_id].delivered is False
    assert by_id[healthy.subscription_id].overflow is False
    assert channel.subscriber_ids == (healthy.subscription_id,)

    # 溢出错误优先于剩余排队投递；后续投递不再到达该订阅者。
    with pytest.raises(EventChannelOverflowError, match="subscription_id="):
        await slow.next()
    assert len(channel.history) == 3
    channel.publish("e4")
    assert channel.subscriber_ids == (healthy.subscription_id,)
    assert [(d.sequence, d.event) for d in healthy.pending()] == [
        (3, "e3"),
        (4, "e4"),
    ]


async def test_pending_raises_after_fail_closed_overflow() -> None:
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(JOB_EVENTS_CHANNEL_KIND, "job_2"),
            overflow_policy="fail_closed",
            max_queue_size=1,
        )
    )
    subscription = channel.subscribe(label="slow")
    channel.publish("e1")
    channel.publish("e2")
    with pytest.raises(EventChannelOverflowError):
        subscription.pending()


def test_sink_subscriber_is_removed_when_offer_returns_false() -> None:
    """外部 sink 订阅者：offer 返回 False 即被 fail_closed 移除。"""
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(JOB_EVENTS_CHANNEL_KIND, "job_3"),
            overflow_policy="fail_closed",
            max_queue_size=4,
            history_size=4,
        )
    )
    received: list[tuple[str, int]] = []

    class FlakySink:
        def __init__(self) -> None:
            self.calls = 0

        def offer(self, event: str, *, sequence: int) -> bool:
            self.calls += 1
            received.append((event, sequence))
            return event != "poison"

    sink = FlakySink()
    subscription_id = channel.subscribe_with_sink(sink, label="external")
    channel.publish("a")
    receipts = channel.publish("poison")
    assert [(r.delivered, r.overflow) for r in receipts] == [(False, True)]
    channel.publish("b")
    # 被移除后不再投递；sequence 保持 channel 级单调。
    assert received == [("a", 1), ("poison", 2)]
    assert sink.calls == 2
    assert channel.subscriber_ids == ()
    assert subscription_id not in channel.subscriber_ids


def test_subscribe_with_sink_requires_fail_closed_channel_and_offer_method() -> None:
    service = EventChannelService()
    gap_channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            overflow_policy="gap",
        )
    )
    with pytest.raises(ValueError, match="只支持 fail_closed"):
        gap_channel.subscribe_with_sink(object(), label="bad")

    fail_closed = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(JOB_EVENTS_CHANNEL_KIND, "job_4"),
            overflow_policy="fail_closed",
        )
    )
    with pytest.raises(TypeError, match="offer"):
        fail_closed.subscribe_with_sink(object(), label="bad")


def test_history_and_cursor_replay() -> None:
    """短期历史 + cursor 重放：按 sequence 重放，环形缓冲覆盖后不可重放。"""
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(JOB_EVENTS_CHANNEL_KIND, "job_5"),
            overflow_policy="fail_closed",
            max_queue_size=8,
            history_size=4,
        )
    )
    for index in range(6):
        channel.publish(f"e{index}")

    assert channel.history == ("e2", "e3", "e4", "e5")
    replayed = channel.replay()
    assert [delivery.event for delivery in replayed] == ["e2", "e3", "e4", "e5"]
    assert [delivery.sequence for delivery in replayed] == [3, 4, 5, 6]
    # e3 的 sequence 是 4：after_sequence=4 重放其后的 e4、e5。
    assert [delivery.event for delivery in channel.replay(after_sequence=4)] == [
        "e4",
        "e5",
    ]
    assert [delivery.event for delivery in channel.replay(limit=1)] == ["e5"]
    assert channel.replay(after_sequence=99) == ()
    # 投递与重放共享同一 sequence，occurred_at 由 channel 盖章。
    assert all(delivery.occurred_at is not None for delivery in replayed)
    with pytest.raises(ValueError, match="limit"):
        channel.replay(limit=0)
    with pytest.raises(ValueError, match="after_sequence"):
        channel.replay(after_sequence=-1)


def test_replay_on_historyless_channel_raises() -> None:
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            overflow_policy="gap",
            history_size=0,
        )
    )
    assert channel.history == ()
    with pytest.raises(EventChannelHistoryDisabledError, match="短期历史"):
        channel.replay()


def test_unsubscribe_stops_delivery_but_keeps_pending() -> None:
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            overflow_policy="gap",
        )
    )
    subscription = channel.subscribe(label="consumer")
    channel.publish("e1")
    assert channel.unsubscribe(subscription) is True
    assert subscription.released is True
    assert channel.subscriber_ids == ()
    assert channel.publish("e2") == ()
    assert [d.event for d in subscription.pending()] == ["e1"]
    assert channel.unsubscribe(subscription) is False
    with pytest.raises(TypeError):
        channel.unsubscribe(123)  # type: ignore[arg-type]


def test_delivery_sequence_is_monotonic_and_shared_per_publish() -> None:
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(RESOURCE_STATE_CHANNEL_KIND, "debug"),
            overflow_policy="gap",
        )
    )
    first = channel.subscribe(label="a")
    second = channel.subscribe(label="b")
    channel.publish("e1")
    channel.publish("e2")
    first_items = first.pending()
    second_items = second.pending()
    assert [d.sequence for d in first_items] == [1, 2]
    assert [d.sequence for d in second_items] == [1, 2]
    assert first_items[0].occurred_at == second_items[0].occurred_at
    assert channel.last_sequence == 2


def test_service_channel_access_and_spec_conflict_fail_fast() -> None:
    service = EventChannelService()
    spec = EventChannelSpec(
        name=channel_name(RESOURCE_STATE_CHANNEL_KIND, "debug"),
        overflow_policy="gap",
        max_queue_size=4,
    )
    channel = service.ensure_channel(spec)
    assert service.channel("resource.state/debug") is channel
    assert service.find_channel("resource.state/debug") is channel
    assert service.find_channel("resource.state/other") is None
    assert service.channel_names == ("resource.state/debug",)
    assert service.channels == (channel,)
    with pytest.raises(KeyError, match="channel 不存在"):
        service.channel("resource.state/other")
    with pytest.raises(EventChannelSpecConflictError, match="spec 冲突"):
        service.ensure_channel(
            EventChannelSpec(
                name=channel_name(RESOURCE_STATE_CHANNEL_KIND, "debug"),
                overflow_policy="gap",
                max_queue_size=8,
            )
        )
    # 完全相同的 spec 幂等返回同一 channel。
    assert service.ensure_channel(spec) is channel


def test_service_and_spec_reject_invalid_arguments() -> None:
    service = EventChannelService()
    with pytest.raises(ValueError, match="'kind/参数' 形式"):
        EventChannelSpec(name="invalid", overflow_policy="gap")
    with pytest.raises(ValueError, match="未知 channel kind"):
        EventChannelSpec(name="unknown.kind/x", overflow_policy="gap")
    with pytest.raises(ValueError, match="overflow_policy"):
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            overflow_policy="best_effort",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="max_queue_size"):
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            max_queue_size=0,
        )
    with pytest.raises(ValueError, match="history_size"):
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            history_size=-1,
        )
    with pytest.raises(TypeError, match="EventChannelSpec"):
        service.ensure_channel("resource.observe/*")  # type: ignore[arg-type]
    channel = service.ensure_channel(
        EventChannelSpec(name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"))
    )
    with pytest.raises(ValueError, match="非空 label"):
        channel.subscribe(label="  ")
    with pytest.raises(ValueError, match="max_queue_size"):
        channel.subscribe(label="a", max_queue_size=0)
    channel.subscribe(label="a", subscription_id="fixed")
    with pytest.raises(ValueError, match="订阅 id 重复"):
        channel.subscribe(label="b", subscription_id="fixed")
    with pytest.raises(ValueError, match="不能为 None"):
        channel.publish(None)  # type: ignore[arg-type]


async def test_next_and_pending_remain_usable_while_released() -> None:
    """释放后已排队投递仍可读取；空队列的 next() 继续等待（既有语义）。"""
    service = EventChannelService()
    channel = service.ensure_channel(
        EventChannelSpec(
            name=channel_name(RESOURCE_OBSERVE_CHANNEL_KIND, "*"),
            overflow_policy="gap",
        )
    )
    subscription = channel.subscribe(label="consumer")
    channel.publish("e1")
    channel.unsubscribe(subscription)
    assert [d.event for d in subscription.pending()] == ["e1"]
    assert subscription.released is True
    # 空队列 + 已释放：next() 仍等待，不抛错（与既有通道行为一致）。
    waiter = asyncio.create_task(subscription.next())
    await asyncio.sleep(0)
    assert not waiter.done()
    waiter.cancel()
