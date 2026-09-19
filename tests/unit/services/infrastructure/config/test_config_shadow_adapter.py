"""workspace 配置 shadow lifecycle 生产适配器合同测试。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.config.shadow_adapter import (
    ConfigShadowLifecycleAdapter,
)
from app.services.infrastructure.config.shadow_scope import (
    ConfigShadowLifecycleError,
)
from app.services.infrastructure.events.channel_events import (
    config_lifecycle_channel_name,
)
from app.services.infrastructure.events.event_channel_service import (
    EventChannelError,
    EventChannelService,
)

_CHANNEL = config_lifecycle_channel_name("workspace")


def _make_adapter(
    *,
    validator_error: Exception | None = None,
) -> tuple[ConfigShadowLifecycleAdapter, EventChannelService, object]:
    """构造真实 EventChannelService + 真实 owner 的适配器与订阅句柄。"""

    def validate(config: Mapping[str, object]) -> None:
        if validator_error is not None:
            raise validator_error
        if "name" not in config:
            raise ValueError("缺少 name")

    async def reconcile(config: Mapping[str, object], scope: LifetimeScope) -> None:
        if config.get("name") == "drain-boom":

            def _boom() -> None:
                raise RuntimeError("旧 generation scope 释放失败")

            scope.register(_boom, label="drain-boom")

    service = EventChannelService()
    adapter = ConfigShadowLifecycleAdapter(
        validator=validate,
        reconcile=reconcile,
        event_service=service,
        bootstrap_guard_keys=("context",),
    )
    channel = service.channel(_CHANNEL)
    subscription = channel.subscribe(label="test-consumer")
    return adapter, service, subscription


def _kind_pairs(subscription: object) -> list[tuple[str, str | None]]:
    deliveries = subscription.pending()
    return [(delivery.event.kind, delivery.event.generation) for delivery in deliveries]


@pytest.mark.asyncio
async def test_bootstrap_and_apply_publish_typed_lifecycle_events() -> None:
    """bootstrap/apply/unchanged 按序发布 typed published/unchanged 事件。"""
    adapter, _service, subscription = _make_adapter()

    active = await adapter.bootstrap({"name": "valid", "context": {}})
    assert adapter.readiness == "ready"
    assert _kind_pairs(subscription) == [("published", "1")]

    result = await adapter.apply_candidate(
        {"name": "valid", "context": {"changed": True}},
        expected_generation=active.generation,
    )
    assert result.status == "published"
    assert result.generation == 2
    assert _kind_pairs(subscription) == [("published", "2")]

    unchanged = await adapter.apply_candidate(
        {"name": "valid", "context": {"changed": True}},
        expected_generation=2,
    )
    assert unchanged.status == "unchanged"
    assert _kind_pairs(subscription) == [("unchanged", "2")]


@pytest.mark.asyncio
async def test_stale_generation_conflict_raises_without_events() -> None:
    """过期 generation fence 拒绝：只抛原始错误，不发任何事件。"""
    adapter, _service, subscription = _make_adapter()
    await adapter.bootstrap({"name": "valid", "context": {}})
    subscription.pending()

    with pytest.raises(ConfigShadowLifecycleError, match="generation_fence_conflict"):
        await adapter.apply_candidate(
            {"name": "valid", "context": {"changed": True}},
            expected_generation=0,
        )
    assert subscription.pending() == ()
    assert adapter.generation == 1


@pytest.mark.asyncio
async def test_event_unavailable_fails_apply_and_keeps_old_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事件通道不可用：发布失败即业务失败，旧 active generation 保留。"""
    adapter, service, subscription = _make_adapter()
    active = await adapter.bootstrap({"name": "valid", "context": {}})
    subscription.pending()
    channel = service.channel(_CHANNEL)

    def _broken_publish(event: object) -> tuple:
        raise EventChannelError("事件通道不可用")

    monkeypatch.setattr(channel, "publish", _broken_publish)

    with pytest.raises(ExceptionGroup) as exc_info:
        await adapter.apply_candidate(
            {"name": "valid", "context": {"changed": True}},
            expected_generation=active.generation,
        )
    # published 事件的通道错误与 failed 事件的通道错误都显式暴露。
    assert all(
        isinstance(error, EventChannelError) for error in exc_info.value.exceptions
    )
    assert adapter.generation == 1
    assert adapter.readiness == "ready"
    assert adapter.active is active


@pytest.mark.asyncio
async def test_old_scope_release_failure_surfaces_and_publishes_failed() -> None:
    """旧 generation scope 排空失败：原始错误不被吞掉，failed 事件显式投递。"""
    adapter, _service, subscription = _make_adapter()
    await adapter.bootstrap({"name": "drain-boom", "context": {}})
    subscription.pending()

    with pytest.raises(ExceptionGroup) as exc_info:
        await adapter.apply_candidate(
            {"name": "valid", "context": {}},
            expected_generation=1,
        )
    release_errors = [
        error
        for error in exc_info.value.exceptions
        if isinstance(error, RuntimeError)
        and error.__cause__ is not None
        and "释放失败" in str(error.__cause__)
    ]
    assert release_errors
    # 新 generation 已原子发布；排空失败以 failed 事件指向当前 generation。
    assert _kind_pairs(subscription) == [
        ("published", "2"),
        ("failed", "2"),
    ]
    assert adapter.generation == 2


@pytest.mark.asyncio
async def test_bootstrap_failure_publishes_failed_event_and_stays_failed() -> None:
    """冷启动校验失败：发布 failed 事件（无 generation），readiness 停在 failed。"""
    adapter, _service, subscription = _make_adapter(
        validator_error=ValueError("bad bootstrap"),
    )

    with pytest.raises(ValueError, match="bad bootstrap"):
        await adapter.bootstrap({"name": "valid"})
    assert _kind_pairs(subscription) == [("failed", None)]
    assert adapter.readiness == "failed"


@pytest.mark.asyncio
async def test_close_publishes_closed_event_once() -> None:
    """close 排空 scope 后发布 closed 事件；重复 close 不重复发布。"""
    adapter, _service, subscription = _make_adapter()
    await adapter.bootstrap({"name": "valid", "context": {}})
    subscription.pending()

    await adapter.close()
    assert _kind_pairs(subscription) == [("closed", "1")]

    await adapter.close()
    assert subscription.pending() == ()
