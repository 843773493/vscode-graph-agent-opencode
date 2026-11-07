"""原 Gateway registry 的管理入口投影，不持有另一套目标登记。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.canonical_params import CanonicalGatewayId


class GatewayManagementTargetDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    gateway_id: CanonicalGatewayId
    name: str
    connection_kind: Literal["local", "remote_gateway"]
    gateway_connection_id: str | None = None
    connection_error: str | None = None


class GatewayManagementDirectoryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    local_gateway_id: CanonicalGatewayId
    items: list[GatewayManagementTargetDTO]
