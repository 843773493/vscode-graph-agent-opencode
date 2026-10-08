from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PortForwardProtocol = Literal["http", "https", "tcp"]


PortForwardStatus = Literal["starting", "active", "error", "stopped"]


class CreatePortForwardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    remote_port: int = Field(ge=1, le=65535)
    local_port: int | None = Field(default=None, ge=1, le=65535)
    protocol: PortForwardProtocol = "http"
    label: str | None = Field(default=None, max_length=120)


class ChangePortForwardLocalPortRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_port: int = Field(ge=1, le=65535)


class ChangePortForwardLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str | None = Field(default=None, max_length=120)


class PortForwardDTO(BaseModel):
    forward_id: str
    workspace_id: str
    connection_id: str
    remote_host: Literal["127.0.0.1"]
    remote_port: int
    local_host: Literal["127.0.0.1"]
    local_port: int
    protocol: PortForwardProtocol
    label: str | None
    status: PortForwardStatus
    error: str | None
    local_url: str | None


class PortForwardListDTO(BaseModel):
    items: list[PortForwardDTO]
