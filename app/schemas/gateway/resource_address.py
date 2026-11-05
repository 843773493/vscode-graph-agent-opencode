"""URI grammar 的公开投影；资源访问仍由对应 owner 核验。"""

from pydantic import BaseModel, ConfigDict, Field


class GatewayResourceAddressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uri: str = Field(min_length=1)
    local_gateway_id: str = Field(min_length=1)
    resource_gateway_id: str | None = Field(default=None, min_length=1)


class GatewayResourceAddressDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    uri: str
    reference_uri: str
    gateway_id: str
    scope: str
    scope_id: str
    kind: str
    file_root_uri: str | None = None
    session_id: str | None = None
    thread_id: str | None = None
