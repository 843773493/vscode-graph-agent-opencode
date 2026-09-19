"""resource.observe/* 轻量通知通道的队列与 gap 合同测试。"""

from __future__ import annotations

import hashlib

import pytest

from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    RESOURCE_OBSERVATION_CHANNEL,
    ResourceObservation,
    ResourceObservationChannel,
    assert_notification_is_lightweight,
)

AGENTS_URI = "boxteam://workspace/agents"


def _revision(label: str) -> str:
    """合法 revision 样例：完整 sha256 摘要（sha256: + 64 位小写 hex）。"""
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def test_channel_delivers_lightweight_observation_per_subscriber() -> None:
    channel = ResourceObservationChannel()
    first = channel.subscribe(label="csm:one")
    second = channel.subscribe(label="csm:two")

    assert channel.channel == RESOURCE_OBSERVATION_CHANNEL
    deliveries = channel.notify(
        ResourceObservation(
            uri="boxteam://workspace/skill/demo",
            revision=_revision("abc"),
            available=True,
        )
    )
    assert [item.gap for item in deliveries] == [False, False]

    for subscription in (first, second):
        pending = subscription.pending()
        assert len(pending) == 1
        assert pending[0].uri == "boxteam://workspace/skill/demo"
        assert pending[0].revision == _revision("abc")
        assert pending[0].gap is False


def test_channel_reports_gap_and_drops_oldest_on_overflow() -> None:
    channel = ResourceObservationChannel(max_queue_size=2)
    subscription = channel.subscribe(label="csm:slow")

    deliveries = channel.notify(
        ResourceObservation(uri=AGENTS_URI, revision=_revision("r1"), available=True)
    )
    assert [item.gap for item in deliveries] == [False]

    # 队列满：丢最旧一条，保留其它来源，并放入 gap 标记。
    deliveries = channel.notify(
        ResourceObservation(
            uri="boxteam://workspace/skill/other",
            revision=_revision("r2"),
            available=True,
        )
    )
    assert [item.gap for item in deliveries] == [False]
    deliveries = channel.notify(
        ResourceObservation(uri=AGENTS_URI, revision=_revision("r3"), available=True)
    )
    assert [item.gap for item in deliveries] == [True]

    pending = subscription.pending()
    assert len(pending) == 2
    # 第一条（r1）被丢弃，其它来源的通知保留，最新一条是 gap 标记。
    assert [(item.uri, item.revision, item.gap) for item in pending] == [
        ("boxteam://workspace/skill/other", _revision("r2"), False),
        (AGENTS_URI, _revision("r3"), True),
    ]


def test_channel_resumes_delivery_after_gap_is_consumed() -> None:
    """M1 回归：溢出后消费 gap 必须恢复投递，不能永久静默。"""
    channel = ResourceObservationChannel(max_queue_size=1)
    subscription = channel.subscribe(label="csm:slow")

    channel.notify(
        ResourceObservation(uri=AGENTS_URI, revision=_revision("r1"), available=True)
    )
    gap_deliveries = channel.notify(
        ResourceObservation(uri=AGENTS_URI, revision=_revision("r2"), available=True)
    )
    assert [item.gap for item in gap_deliveries] == [True]

    pending = subscription.pending()
    assert len(pending) == 1
    assert pending[0].gap is True

    # gap 已消费：后续通知必须重新进入队列，而不是被静默丢弃。
    deliveries = channel.notify(
        ResourceObservation(uri=AGENTS_URI, revision=_revision("r3"), available=True)
    )
    assert [item.gap for item in deliveries] == [False]
    resumed = subscription.pending()
    assert [(item.revision, item.gap) for item in resumed] == [(_revision("r3"), False)]


def test_channel_keeps_single_gap_marker_while_overflow_persists() -> None:
    channel = ResourceObservationChannel(max_queue_size=1)
    subscription = channel.subscribe(label="csm:slow")

    channel.notify(
        ResourceObservation(uri=AGENTS_URI, revision=_revision("r1"), available=True)
    )
    assert [
        item.gap
        for item in channel.notify(
            ResourceObservation(uri=AGENTS_URI, revision=_revision("r2"), available=True)
        )
    ] == [True]
    # 溢出状态持续期间不重复堆积 gap 标记，但每次投递都显式报告 gap。
    for revision in ("r3", "r4"):
        deliveries = channel.notify(
            ResourceObservation(uri=AGENTS_URI, revision=_revision(revision), available=True)
        )
        assert [item.gap for item in deliveries] == [True]

    pending = subscription.pending()
    assert len(pending) == 1
    assert pending[0].gap is True


@pytest.mark.asyncio
async def test_channel_resumes_delivery_after_gap_consumed_via_next() -> None:
    """异步 `next()` 消费 gap 后同样恢复投递。"""
    channel = ResourceObservationChannel(max_queue_size=1)
    subscription = channel.subscribe(label="csm:slow")

    channel.notify(
        ResourceObservation(uri=AGENTS_URI, revision=_revision("r1"), available=True)
    )
    channel.notify(
        ResourceObservation(uri=AGENTS_URI, revision=_revision("r2"), available=True)
    )
    first = await subscription.next()
    assert first.gap is True

    deliveries = channel.notify(
        ResourceObservation(uri=AGENTS_URI, revision=_revision("r3"), available=True)
    )
    assert [item.gap for item in deliveries] == [False]
    resumed = await subscription.next()
    assert (resumed.revision, resumed.gap) == (_revision("r3"), False)


def test_unsubscribe_stops_delivery() -> None:
    channel = ResourceObservationChannel()
    subscription = channel.subscribe(label="csm:one")

    assert channel.unsubscribe(subscription) is True
    assert subscription.released is True
    assert channel.subscriber_ids == ()
    assert (
        channel.notify(
            ResourceObservation(uri=AGENTS_URI, revision=_revision("r1"), available=True)
        )
        == ()
    )
    assert subscription.pending() == ()
    assert channel.unsubscribe(subscription) is False


def test_notification_value_and_field_shape_are_validated() -> None:
    """M4：字段集合按 dataclass 定义校验，值形状也校验，不是恒真守卫。"""
    observation = ResourceObservation(
        uri=AGENTS_URI,
        revision=_revision("r1"),
        available=True,
    )
    assert_notification_is_lightweight(observation)

    # 值形状：uri 必须是 boxteam:// 虚拟 identity，revision 必须是 sha256 摘要。
    with pytest.raises(RuntimeError, match="boxteam://"):
        assert_notification_is_lightweight(
            ResourceObservation(uri="/host/path/AGENTS.md", revision=_revision("r1"), available=True)
        )
    with pytest.raises(RuntimeError, match="sha256"):
        assert_notification_is_lightweight(
            ResourceObservation(uri=AGENTS_URI, revision="r1", available=True)
        )

    # dataclass 值对象层面的构造校验。
    with pytest.raises(ValueError):
        ResourceObservation(uri="", revision=_revision("r1"), available=True)
    with pytest.raises(ValueError):
        ResourceObservation(uri=AGENTS_URI, revision="", available=True)

    # R2b 复核 M1-R：uri 超长（把正文塞进 uri）与 '..' 相对路径形状必须显式报错。
    with pytest.raises(RuntimeError, match="长度上限"):
        assert_notification_is_lightweight(
            ResourceObservation(
                uri="boxteam://workspace/" + "正文" * 2000,
                revision=_revision("r1"),
                available=True,
            )
        )
    with pytest.raises(RuntimeError, match=r"\.\."):
        assert_notification_is_lightweight(
            ResourceObservation(
                uri="boxteam://workspace/../../etc/passwd",
                revision=_revision("r1"),
                available=True,
            )
        )