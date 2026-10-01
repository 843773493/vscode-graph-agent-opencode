from __future__ import annotations

from app.protocol.generated.boxteam.common.v1 import service_lifecycle_pb2
from app.protocol.generated.boxteam.gateway.v1 import health_pb2, workspace_registry_pb2
from app.schemas.gateway import GatewayHealthDTO, GatewayWorkspaceListDTO


def gateway_health_to_proto(
    value: GatewayHealthDTO,
    *,
    gateway_id: str,
) -> health_pb2.GatewayHealth:
    status = health_pb2.GatewayHealth(
        gateway_id=gateway_id,
        status=service_lifecycle_pb2.SERVICE_STATUS_READY
        if value.status == "ok"
        else service_lifecycle_pb2.SERVICE_STATUS_UNSPECIFIED,
        process_id=value.process_id,
        development_restart_available=value.development_restart_available,
    )
    if value.active_workspace_id is not None:
        status.active_workspace_id = value.active_workspace_id
    return status


def gateway_workspace_list_to_proto(
    value: GatewayWorkspaceListDTO,
) -> workspace_registry_pb2.WorkspaceRegistry:
    registry = workspace_registry_pb2.WorkspaceRegistry()
    if value.active_workspace_id is not None:
        registry.active_workspace_id = value.active_workspace_id
    for workspace in value.items:
        status_name = f"WORKSPACE_STATUS_{workspace.status.upper()}"
        status = getattr(workspace_registry_pb2, status_name, None)
        if not isinstance(status, int):
            raise TypeError(f"Gateway 工作区状态无法映射到 Protobuf: {workspace.status}")
        registry.items.add(
            workspace_id=workspace.workspace_id,
            name=workspace.name,
            root_path=workspace.root_path,
            backend_url=workspace.backend_url,
            status=status,
            active=workspace.active,
            connection_kind=workspace.connection_kind,
        )
    return registry
