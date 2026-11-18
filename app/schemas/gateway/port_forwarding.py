from __future__ import annotations

from ipaddress import ip_address
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PortForwardProtocol = Literal["http", "https", "tcp"]


PortForwardStatus = Literal["starting", "active", "error", "stopped"]


class PortForwardEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    host_id: str = Field(min_length=1)
    address: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if any(character.isspace() or character in "[]/\\" for character in value):
            raise ValueError("地址必须是 IP 或主机名，不包含协议、路径或方括号")
        if ":" in value:
            ip_address(value)
        return value


class CreatePortForwardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    listener: PortForwardEndpoint
    destination: PortForwardEndpoint
    protocol: PortForwardProtocol = "tcp"
    label: str | None = Field(default=None, max_length=120)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_listener(self) -> CreatePortForwardRequest:
        ip_address(self.listener.address)
        if self.listener == self.destination:
            raise ValueError("监听端和服务端不能完全相同")
        return self


class PortForwardDTO(CreatePortForwardRequest):
    forward_id: str
    gateway_id: str
    status: PortForwardStatus
    error: str | None


class PortForwardListDTO(BaseModel):
    gateway_id: str
    items: list[PortForwardDTO]


class PortForwardHostDTO(BaseModel):
    host_id: str
    label: str
    description: str
    kind: Literal["local", "ssh"]


class PortForwardHostsDTO(BaseModel):
    gateway_id: str
    items: list[PortForwardHostDTO]
