"""同一 Workspace 经已登记的直连 Gateway channel 读取真实 Skill owner。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.core.resource_uri import ResourceUriGrammarError
from app.gateway.federation.errors import (
    FEDERATION_CAPABILITY_DENIED,
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_UNKNOWN_PEER,
    FederationError,
)
from app.gateway.federation.messages import (
    FEDERATION_DEADLINE_SECONDS,
    remaining_deadline,
)
from app.gateway.federation.ports import new_origin_nonce
from app.gateway.federation.protocol import METHOD_SKILL_SOURCES, require_str
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)

if TYPE_CHECKING:
    from app.gateway.federation.channel import FederationChannelSession
    from app.gateway.federation.rpc import FederationRpcService


class GatewaySkillSourcePort:
    """只暴露本 owner 的固定来源和本机 Workspace audience，不接受 endpoint。"""

    def __init__(self, *, owner: GatewaySkillSourceOwner, registry: GatewayWorkspaceRegistry) -> None:
        self.owner = owner
        self._registry = registry

    def accepts_workspace(self, workspace_id: str) -> bool:
        return any(
            target.connection_kind == "local" and target.backend_workspace_id == workspace_id
            for target in self._registry.targets()
        )

    async def execute(self, request: dict[str, object]) -> dict[str, object]:
        gateway_id = require_str(request, "gateway_id")
        workspace_id = require_str(request, "workspace_id")
        identity = {
            "gateway_id": self.owner.gateway_id, "workspace_id": workspace_id,
            "request_id": require_str(request, "request_id"),
        }
        if gateway_id != self.owner.gateway_id:
            return {**identity, "status": 409, "detail": "Gateway Skill source owner mismatch"}
        if not self.accepts_workspace(workspace_id):
            return {**identity, "status": 403, "detail": "Workspace is outside Gateway Skill source audience"}
        kind = require_str(request, "kind")
        if kind == "snapshot":
            snapshot = self.owner.snapshot(workspace_id=workspace_id)
            etag = '"' + snapshot.revision + '"'
            if request.get("if_none_match") == etag:
                return {**identity, "status": 304, "etag": etag}
            return {**identity, "status": 200, "etag": etag, "data": snapshot.model_dump(mode="json")}
        if kind != "navigation":
            raise FederationError(FEDERATION_MALFORMED_FRAME, "Gateway Skill source 请求类型无效")
        navigation = ResourceNavigationRequest.model_validate(request.get("navigation"))
        try:
            result = await self.owner.navigate(navigation)
        except ResourceUriGrammarError as error:
            return {**identity, "status": 400, "detail": {"owner": "resource_uri_grammar", "code": error.reason_code, "message": str(error)}}
        except ResourceUriResolveError as error:
            return {**identity, "status": 404 if error.reason_code == "unknown_resource" else 403,
                    "detail": {"owner": "resource_uri_resolver", "code": error.reason_code, "message": str(error)}}
        except ResourceFileOperationError as error:
            return {**identity, "status": 400, "detail": error.detail()}
        except (OSError, UnicodeError) as error:
            return {**identity, "status": 400, "detail": {"owner": "resource_navigation", "type": type(error).__name__, "errno": getattr(error, "errno", None)}}
        return {**identity, "status": 200, "data": result.model_dump(mode="json")}


async def relay_gateway_skill_sources(
    service: FederationRpcService, request: dict[str, object],
) -> dict[str, object]:
    """可信入口只向 registry 唯一登记的直接 spoke 发起请求，绝不猜 URL。"""
    gateway_id = require_str(request, "gateway_id")
    workspace_id = require_str(request, "workspace_id")
    port = service.gateway_skill_sources
    if port is None or not port.accepts_workspace(workspace_id):
        raise FederationError(FEDERATION_CAPABILITY_DENIED, "源 Workspace 不属于可信入口 Gateway 的本机 audience")
    channel = _registered_owner_channel(service, gateway_id)
    # 这是独立的 Gateway 来源读取协议；不把 Skill 伪装成 Session read grant。
    service._authorize(capability="read", origin=service.gateway_id, session=channel)
    deadline = time.time() + FEDERATION_DEADLINE_SECONDS
    result = await channel.request(
        method=METHOD_SKILL_SOURCES,
        request={**request, "origin_gateway_id": service.gateway_id, "nonce": new_origin_nonce()},
        timeout=remaining_deadline(deadline), deadline_at=deadline,
    )
    if _registered_owner_channel(service, gateway_id) is not channel:
        raise FederationError(FEDERATION_UNKNOWN_PEER, "Gateway Skill source 返回前 channel 已更换")
    service._authorize(capability="read", origin=service.gateway_id, session=channel)
    for field in ("gateway_id", "workspace_id", "request_id"):
        if result.get(field) != request.get(field):
            raise FederationError(FEDERATION_MALFORMED_FRAME, f"Gateway Skill source 回执 {field} 漂移")
    return result


def _registered_owner_channel(service: FederationRpcService, gateway_id: str) -> FederationChannelSession:
    directory = service.spoke_directory
    matches = [spoke for spoke in directory.active_spokes() if spoke.gateway_id == gateway_id] if directory is not None else []
    if len(matches) != 1:
        raise FederationError(FEDERATION_UNKNOWN_PEER, "Gateway Skill owner 未唯一登记为直接 spoke")
    channel = service.channel_to(gateway_id, qualified=True)
    if channel.local_role != "hub" or channel.handshake.peer.connection_id != matches[0].connection_id:
        raise FederationError(FEDERATION_UNKNOWN_PEER, "Gateway Skill owner 的 channel 与登记连接不一致")
    return channel


async def serve_gateway_skill_sources(
    service: FederationRpcService, request: dict[str, object],
    *, session: FederationChannelSession, deadline_at: float | None,
) -> dict[str, object]:
    """spoke 仅接受当前唯一 hub 直达自己的来源读取，不转发到任何 Gateway。"""
    remaining_deadline(deadline_at)
    if session.local_role != "spoke" or service.hub_channel is not session:
        raise FederationError(FEDERATION_CAPABILITY_DENIED, "Gateway Skill source 只允许唯一 hub 直达 owner")
    if request.get("origin_gateway_id") != session.peer_gateway_id or request.get("gateway_id") != service.gateway_id:
        raise FederationError(FEDERATION_UNKNOWN_PEER, "Gateway Skill source origin/target owner mismatch")
    service._authorize(capability="read", origin=session.peer_gateway_id, session=session)
    service._adopt_origin_envelope(request=request, session=session, grant_kind="gateway_skill_sources")
    if service.gateway_skill_sources is None:
        raise FederationError(FEDERATION_CAPABILITY_DENIED, "Gateway Skill source owner 未装配")
    result = await service.gateway_skill_sources.execute(request)
    remaining_deadline(deadline_at)
    service._authorize(capability="read", origin=session.peer_gateway_id, session=session)
    return result
