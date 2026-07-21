from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class McpToolDTO(BaseModel):
    tool_id: str
    server_id: str
    remote_name: str
    description: str


class McpServerDTO(BaseModel):
    server_id: str
    transport: Literal["stdio", "streamable_http"]
    enabled: bool
    status: Literal["disabled", "ready"]
    tools: list[McpToolDTO] = Field(default_factory=list)
