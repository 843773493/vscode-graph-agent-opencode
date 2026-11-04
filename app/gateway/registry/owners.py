"""控制面发现和联邦查询共用登记的原生工作区归属。"""

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.gateway.search import GatewaySearchWorkspaceOwnerDTO


def workspace_owner(
    target: WorkspaceTarget, registry: GatewayWorkspaceRegistry, gateway_id: str,
) -> GatewaySearchWorkspaceOwnerDTO:
    return GatewaySearchWorkspaceOwnerDTO(gateway_id=target.gateway_id, workspace_id=target.workspace_id)
