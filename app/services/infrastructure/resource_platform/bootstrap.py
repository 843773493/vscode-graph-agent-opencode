"""resource platform 的进程级固定装配。

每个 owner 进程只在本模块按代码装配一次：process-root LifetimeScope、
资源观察事件通道、内置文件快照能力（共享 watcher + 稳定读取 registry），
没有动态 provider 注册：一切适配在构造参数里固定，测试通过注入替身
端口替换实际 owner。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.abstractions.resource_file_content import ResourceFileReader
from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)
from app.services.infrastructure.resource_platform.sources.configured_skill_sources import (
    ConfiguredSkillSources,
)
from app.services.infrastructure.resource_platform.sources.gateway_resource_navigation import (
    GatewayResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
    GatewaySkillSources,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.skill_source_config import (
    validate_skill_sources,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)

logger = logging.getLogger(__name__)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

_RESOURCE_STATE_OWNER = "workspace_resource_platform"
_RESOURCE_STATE_ID = "workspace_resource_platform_root"


@dataclass(frozen=True, slots=True)
class ResourcePlatform:
    """一次固定装配的结果；生命周期统一由 process_root_scope 关闭。"""

    process_root_scope: LifetimeScope
    observation_channel: ResourceObservationChannel
    file_watch_service: WorkspaceFileWatchService
    file_registry: WorkspaceFileResourceRegistry
    state_events: ResourceStateEventPublisher
    gateway_skill_sources: GatewaySkillSources | None
    configured_skill_sources: ConfiguredSkillSources | None
    navigation: ResourceNavigation | None

    async def prepare_config(self, config: Mapping[str, object], scope: LifetimeScope) -> Callable[[], None]:
        """由已有配置 shadow 持有候选订阅，accepted 来源继续由原 owner 管理。"""
        resources = cast(Mapping[str, object], config["resources"])
        skills = cast(Mapping[str, object], resources["skill_sources"])
        policy = SourceUpdatePolicy.model_validate(skills["gateway_updates"])
        configs = validate_skill_sources(skills.get("sources", []))
        if configs and self.configured_skill_sources is None:
            raise RuntimeError("配置 Skill 配置缺少精确 Workspace owner")
        activate_sources = await self.configured_skill_sources.prepare_config(configs, scope) if self.configured_skill_sources is not None else None
        activate_gateway = self.gateway_skill_sources.prepare_update_policy(policy, scope) if self.gateway_skill_sources is not None else None

        def activate() -> None:
            if activate_sources is not None:
                activate_sources()
            if activate_gateway is not None:
                activate_gateway()

        return activate

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
    event_service: EventChannelService | None = None,
    workspace_id: str | None = None,
    gateway_connection: GatewayTransportConnection | None = None,
    gateway_token_supplier: Callable[[], str] | None = None,
    file_paths: WorkspaceFilePaths | None = None,
    artifact_reader: ResourceFileReader | None = None,
    gateway_skill_updates: SourceUpdatePolicy | None = None,
) -> ResourcePlatform:
    """按固定顺序装配进程级资源平台。

    登记顺序即关闭逆序：file registry 先停（停止消费），随后共享
    watcher 服务。Gateway/Workspace 配置域互不接管：本装配只启动本进程
    配置域的文件快照能力，不读取对方配置。
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
    scope.register(
        watch_service.shutdown,
        label="workspace-file-watch-service",
    )
    scope.register(
        file_registry.stop,
        label="workspace-file-resource-registry",
    )
    gateway_skill_sources = None
    gateway_navigation = None
    if (gateway_connection is not None or gateway_token_supplier is not None) and not all(
        value is not None for value in (gateway_connection, gateway_token_supplier, workspace_id)
    ):
        raise ValueError("Gateway Skill source 装配必须同时提供连接、凭据供应方和Workspace身份")
    if gateway_connection is not None and gateway_token_supplier is not None and workspace_id is not None:
        gateway_navigation = GatewayResourceNavigation(workspace_id=workspace_id,
            connection=gateway_connection, local_token_supplier=gateway_token_supplier)
        gateway_skill_sources = GatewaySkillSources(
            workspace_id=workspace_id, connection=gateway_connection,
            local_token_supplier=gateway_token_supplier, observation_channel=observation_channel,
            update_policy=gateway_skill_updates,
        )
        scope.register(gateway_skill_sources.close, label="gateway-skill-sources")
    configured_skill_sources = ConfiguredSkillSources(scope="workspace", scope_id=workspace_id, channel=observation_channel) if workspace_id is not None else None
    if configured_skill_sources is not None:
        scope.register(configured_skill_sources.close, label="workspace-http-skills")
    return ResourcePlatform(
        process_root_scope=scope,
        observation_channel=observation_channel,
        file_watch_service=watch_service,
        file_registry=file_registry,
        state_events=state_events,
        gateway_skill_sources=gateway_skill_sources,
        configured_skill_sources=configured_skill_sources,
        navigation=ResourceNavigation(
            workspace_root=workspace_root, workspace_id=workspace_id,
            project_root=project_root, gateway_sources=gateway_skill_sources, file_paths=file_paths,
            gateway_navigation=gateway_navigation,
            artifact_reader=artifact_reader,
            network_skill_reader=configured_skill_sources.read if configured_skill_sources is not None else None,
        ) if workspace_id is not None else None,
    )


__all__ = [
    "ResourcePlatform",
    "bootstrap_resource_platform",
]
