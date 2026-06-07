from __future__ import annotations

from pydantic import BaseModel, Field


class ToolDTO(BaseModel):
    tool_id: str
    name: str
    description: str | None = None
    parameters: dict[str, object] = Field(default_factory=dict)
    category: str | None = None


class ToolInvokeRequest(BaseModel):
    parameters: dict[str, object] = Field(default_factory=dict)


class ToolInvokeResultDTO(BaseModel):
    tool_id: str
    status: str
    result: str
    parameters: dict[str, object] = Field(default_factory=dict)
