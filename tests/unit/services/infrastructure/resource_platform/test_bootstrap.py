"""resource_platform bootstrap 固定装配与内置适配器的单元测试。"""

from __future__ import annotations

import pytest

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
)
from app.services.infrastructure.resource_platform.bootstrap import (
    bootstrap_resource_platform,
)


def test_bootstrap_assembles_platform_with_fixed_adapters(tmp_path) -> None:
    """bootstrap 固定装配：scope、事件通道与 file 能力。"""
    platform = bootstrap_resource_platform(workspace_root=tmp_path)
    # 事件通道与 file registry 共用同一实例，不建第二套事件面。
    assert platform.observation_channel is platform.file_registry.observation_channel


@pytest.mark.asyncio
async def test_bootstrap_scope_close_releases_in_reverse_order(tmp_path) -> None:
    """进程根 scope 关闭时按登记逆序释放：registry -> watch -> monitor。"""
    service = EventChannelService()
    platform = bootstrap_resource_platform(
        workspace_root=tmp_path,
        event_service=service,
    )
    monitor = platform.shared_file_monitor
    watch_service = platform.file_watch_service
    registry = platform.file_registry
    subscription = service.channel(
        "resource.state/workspace_resource_platform"
    ).subscribe(label="test")
    await platform.close()
    # 重复 close 幂等。
    await platform.process_root_scope.close()
    assert monitor.closed
    assert watch_service._watchers == {}
    assert registry._task is None
    # 关闭后拒绝新登记。
    with pytest.raises(RuntimeError, match="不允许注册"):
        platform.process_root_scope.register(lambda: None)
    assert isinstance(platform.process_root_scope, LifetimeScope)
    deliveries = subscription.pending()
    assert [delivery.event.state for delivery in deliveries] == ["released"]


@pytest.mark.asyncio
async def test_bootstrap_close_publishes_release_failure(tmp_path) -> None:
    """释放失败保留原始错误，并由实际 owner 发布 release_failed。"""
    service = EventChannelService()
    platform = bootstrap_resource_platform(
        workspace_root=tmp_path,
        event_service=service,
    )
    subscription = service.channel(
        "resource.state/workspace_resource_platform"
    ).subscribe(label="test")

    def fail_release() -> None:
        raise RuntimeError("release boom")

    platform.process_root_scope.register(fail_release, label="failing-resource")
    with pytest.raises(ExceptionGroup, match="LifetimeScope close"):
        await platform.close()
    deliveries = subscription.pending()
    assert [delivery.event.state for delivery in deliveries] == ["release_failed"]


@pytest.mark.asyncio
async def test_bootstrap_close_event_failure_is_explicit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通知不可用不改变已释放事实，但 owner 必须显式抛出事件错误。"""
    service = EventChannelService()
    platform = bootstrap_resource_platform(
        workspace_root=tmp_path,
        event_service=service,
    )
    channel = service.channel("resource.state/workspace_resource_platform")

    def fail_publish(_event) -> None:
        raise RuntimeError("event unavailable")

    monkeypatch.setattr(channel, "publish", fail_publish)
    with pytest.raises(RuntimeError, match="event unavailable"):
        await platform.close()
    assert platform.process_root_scope.snapshot().state == "closed"

