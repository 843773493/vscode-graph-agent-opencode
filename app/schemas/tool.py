from pydantic import BaseModel, Field
from typing import Any, Optional, List


class ToolDTO(BaseModel):
    tool_id: str
    name: str
    description: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    category: Optional[str] = None


class ToolInvokeRequest(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolInvokeResultDTO(BaseModel):
    tool_id: str
    status: str
    result: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolInvokeResultDTO(BaseModel):
    tool_id: str
    status: str
    result: str
    parameters: dict[str, Any] = Field(default_factory=dict)
