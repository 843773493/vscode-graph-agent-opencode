from __future__ import annotations

from app.protocol.generated.boxteam.common.v1 import service_lifecycle_pb2
from app.protocol.generated.boxteam.gateway.v1 import health_pb2
from app.schemas.gateway import GatewayHealthDTO


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

