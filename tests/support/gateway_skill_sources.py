"""Skill 单元测试的显式 Gateway 原始发布事实；不模拟 Workspace 本机回读。"""

from __future__ import annotations

from pathlib import Path

from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
    GatewaySkillSources,
)


def accepted_gateway_skill_sources(
    *, skills_root: Path, gateway_id: str, workspace_id: str,
    observation_channel: ResourceObservationChannel,
) -> GatewaySkillSources:
    """由真实文件 owner 读原始事实，再走唯一的 typed 接受校验。"""
    producer = GatewaySkillSourceOwner(gateway_id=gateway_id, skills_root=skills_root)
    producer._publish()
    consumer = GatewaySkillSources(
        workspace_id=workspace_id, connection=GatewayTransportConnection(gateway_url="http://gateway.invalid"),
        local_token_supplier=lambda: "fixture-local-token", observation_channel=observation_channel,
    )
    consumer._gateway_id = gateway_id
    consumer._accept_snapshot(producer.snapshot(workspace_id=workspace_id))
    return consumer
