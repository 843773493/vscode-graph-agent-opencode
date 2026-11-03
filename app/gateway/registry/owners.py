"""控制面发现和联邦查询共用稳定工作区归属，不把路由别名作为身份。"""

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.gateway.search import GatewaySearchWorkspaceOwnerDTO


def workspace_owner(
    target: WorkspaceTarget, registry: GatewayWorkspaceRegistry, gateway_id: str,
) -> GatewaySearchWorkspaceOwnerDTO:
    if target.backend_workspace_id is None:
        raise ValueError(f"已登记目标缺少工作区身份: {target.workspace_id}")
    if target.connection_kind == "remote_gateway":
        if target.remote_gateway_connection_id is None:
            raise ValueError(f"已登记远端目标缺少所属身份: {target.workspace_id}")
        connection = registry.remote_gateway_connection(target.remote_gateway_connection_id)
        return GatewaySearchWorkspaceOwnerDTO(
            gateway_id=connection.remote_gateway_id, workspace_id=target.backend_workspace_id,
        )
    return GatewaySearchWorkspaceOwnerDTO(gateway_id=gateway_id, workspace_id=target.backend_workspace_id)
