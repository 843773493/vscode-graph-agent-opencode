"""resource_platform bootstrap 固定装配与内置适配器的单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
)
from app.services.infrastructure.resource_platform.adapters.gateway_snapshot import (
    AuthenticatedGatewaySnapshot,
)
from app.services.infrastructure.resource_platform.adapters.memory_state import (
    AuthoritativeMemorySnapshot,
)
from app.services.infrastructure.resource_platform.bootstrap import (
    bootstrap_resource_platform,
)


class _FakeGatewayReader:
    """按 locator 返回固定受认证快照的替身 owner。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def read_snapshot(self, locator: str) -> AuthenticatedGatewaySnapshot:
        self.calls.append(locator)
        return AuthenticatedGatewaySnapshot(
            locator=locator,
            version_token=f"token:{locator}",
            content=b"snapshot-bytes",
        )


class _FakeMemoryReader:
    """按 key 返回固定权威内存状态的替身 owner。"""

    async def read_state(self, key: str) -> AuthoritativeMemorySnapshot:
        return AuthoritativeMemorySnapshot(
            key=key,
            version_token=f"token:{key}",
            payload={"state": key},
        )


def test_bootstrap_assembles_platform_with_fixed_adapters(tmp_path) -> None:
    """bootstrap 固定装配：scope、事件通道、file 能力与可选适配。"""
    gateway_reader = _FakeGatewayReader()
    memory_reader = _FakeMemoryReader()
    platform = bootstrap_resource_platform(
        workspace_root=tmp_path,
        gateway_snapshot_reader=gateway_reader,
        gateway_snapshot_locators=("boxteam://gateway/skills",),
        memory_state_reader=memory_reader,
        memory_state_keys=("team_state",),
    )
    assert platform.observation_channel is platform.file_registry.observation_channel
    assert platform.gateway_snapshots is not None
    assert platform.memory_states is not None
    # 事件通道与 file registry 共用同一实例，不建第二套事件面。
    snapshot = asyncio.run(
        platform.gateway_snapshots.snapshot("boxteam://gateway/skills")
    )
    assert snapshot.content == b"snapshot-bytes"
    state = asyncio.run(platform.memory_states.state("team_state"))
    assert state.payload == {"state": "team_state"}


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


@pytest.mark.asyncio
async def test_gateway_snapshot_adapter_rejects_unregistered_locator() -> None:
    """未在固定装配登记的 locator 显式失败；空 token 显式失败。"""
    platform = bootstrap_resource_platform(
        workspace_root=Path("/tmp"),
        gateway_snapshot_reader=_FakeGatewayReader(),
        gateway_snapshot_locators=("boxteam://gateway/skills",),
    )
    adapter = platform.gateway_snapshots
    assert adapter is not None
    with pytest.raises(KeyError, match="未在固定装配中登记"):
        await adapter.snapshot("boxteam://gateway/other")

    class _EmptyTokenReader(_FakeGatewayReader):
        async def read_snapshot(self, locator: str) -> AuthenticatedGatewaySnapshot:
            return AuthenticatedGatewaySnapshot(
                locator=locator,
                version_token="",
                content=b"",
            )

    from app.services.infrastructure.resource_platform.adapters.gateway_snapshot import (
        GatewaySnapshotAdapter,
    )

    strict_adapter = GatewaySnapshotAdapter(
        reader=_EmptyTokenReader(),
        locators=("boxteam://gateway/skills",),
    )
    with pytest.raises(RuntimeError, match="version token"):
        await strict_adapter.snapshot("boxteam://gateway/skills")


@pytest.mark.asyncio
async def test_memory_state_adapter_rejects_unregistered_key() -> None:
    """未登记的 memory key 显式失败，不返回伪造默认状态。"""
    platform = bootstrap_resource_platform(
        workspace_root=Path("/tmp"),
        memory_state_reader=_FakeMemoryReader(),
        memory_state_keys=("team_state",),
    )
    adapter = platform.memory_states
    assert adapter is not None
    with pytest.raises(KeyError, match="未在固定装配中登记"):
        await adapter.state("unknown_key")
