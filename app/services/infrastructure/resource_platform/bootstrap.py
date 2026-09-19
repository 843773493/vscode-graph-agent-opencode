"""resource platform 的进程级固定装配。

每个 owner 进程只在本模块按代码装配一次：process-root LifetimeScope、
资源观察事件通道、内置文件快照能力（共享监视 + 稳定读取 registry），
以及按 owner 需要显式传入的 Gateway 受认证快照与权威内存状态适配。
没有动态 provider 注册：一切适配在构造参数里固定，测试通过注入替身
端口替换实际 owner。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
)
from app.services.infrastructure.resource_platform.adapters.file_monitor import (
    SharedFileMonitor,
    WorkspaceFileWatchPort,
)
from app.services.infrastructure.resource_platform.adapters.gateway_snapshot import (
    GatewaySnapshotAdapter,
    GatewaySnapshotReader,
)
from app.services.infrastructure.resource_platform.adapters.memory_state import (
    MemoryStateAdapter,
    MemoryStateReader,
)
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)

logger = logging.getLogger(__name__)

_RESOURCE_STATE_OWNER = "workspace_resource_platform"
_RESOURCE_STATE_ID = "workspace_resource_platform_root"


@dataclass(frozen=True, slots=True)
class ResourcePlatform:
    """一次固定装配的结果；生命周期统一由 process_root_scope 关闭。"""

    process_root_scope: LifetimeScope
    observation_channel: ResourceObservationChannel
    file_watch_service: WorkspaceFileWatchService
    file_registry: WorkspaceFileResourceRegistry
    shared_file_monitor: SharedFileMonitor
    state_events: ResourceStateEventPublisher
    gateway_snapshots: GatewaySnapshotAdapter | None = None
    memory_states: MemoryStateAdapter | None = None

    async def close(self) -> None:
        """释放实际持有的进程资源并发布 owner 已确认的轻量状态。"""
        try:
            await self.process_root_scope.close()
        except ExceptionGroup as release_error:
            try:
                self.state_events.publish(
                    resource_id=_RESOURCE_STATE_ID,
                    state="release_failed",
                )
            except (RuntimeError, ValueError) as event_error:
                logger.exception(
                    "resource platform 释放失败且状态事件发布失败",
                    exc_info=event_error,
                )
                raise ExceptionGroup(
                    "resource platform 释放与状态通知均失败",
                    [release_error, event_error],
                ) from release_error
            raise
        try:
            self.state_events.publish(
                resource_id=_RESOURCE_STATE_ID,
                state="released",
            )
        except (RuntimeError, ValueError) as event_error:
            logger.exception(
                "resource platform 已释放，但 released 状态事件发布失败",
                exc_info=event_error,
            )
            raise


def bootstrap_resource_platform(
    *,
    workspace_root: Path,
    project_root: Path | None = None,
    process_scope_name: str = "resource-platform-root",
    gateway_snapshot_reader: GatewaySnapshotReader | None = None,
    gateway_snapshot_locators: tuple[str, ...] = (),
    memory_state_reader: MemoryStateReader | None = None,
    memory_state_keys: tuple[str, ...] = (),
    event_service: EventChannelService | None = None,
) -> ResourcePlatform:
    """按固定顺序装配进程级资源平台。

    登记顺序即关闭逆序：file registry 先停（停止消费），随后共享
    watcher 服务，最后是共享监视协调器。Gateway/Workspace 配置域互不
    接管：本装配只启动本进程配置域的文件快照能力，不读取对方配置。
    """
    scope = LifetimeScope(process_scope_name)
    watch_service = WorkspaceFileWatchService(workspace_root=workspace_root)
    shared_event_service = event_service or EventChannelService()
    observation_channel = ResourceObservationChannel(
        event_service=shared_event_service,
    )
    state_events = ResourceStateEventPublisher(
        event_service=shared_event_service,
        owner_domain=_RESOURCE_STATE_OWNER,
    )
    file_registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace_root,
        watch_service=watch_service,
        project_root=project_root,
        observation_channel=observation_channel,
    )
    shared_file_monitor = SharedFileMonitor(
        port=WorkspaceFileWatchPort(watch_service),
        instance_id=f"{process_scope_name}:file-monitor",
    )
    scope.register(
        shared_file_monitor.close,
        label="shared-file-monitor",
    )
    scope.register(
        watch_service.shutdown,
        label="workspace-file-watch-service",
    )
    scope.register(
        file_registry.stop,
        label="workspace-file-resource-registry",
    )
    gateway_snapshots: GatewaySnapshotAdapter | None = None
    if gateway_snapshot_reader is not None:
        gateway_snapshots = GatewaySnapshotAdapter(
            reader=gateway_snapshot_reader,
            locators=gateway_snapshot_locators,
        )
    memory_states: MemoryStateAdapter | None = None
    if memory_state_reader is not None:
        memory_states = MemoryStateAdapter(
            reader=memory_state_reader,
            keys=memory_state_keys,
        )
    return ResourcePlatform(
        process_root_scope=scope,
        observation_channel=observation_channel,
        file_watch_service=watch_service,
        file_registry=file_registry,
        shared_file_monitor=shared_file_monitor,
        state_events=state_events,
        gateway_snapshots=gateway_snapshots,
        memory_states=memory_states,
    )


__all__ = [
    "ResourcePlatform",
    "bootstrap_resource_platform",
]
