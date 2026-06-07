from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ConfigDTO(BaseModel):
    default_model: str
    default_orchestration: str
    max_concurrent_agents: int = 4
    allow_shell_tools: bool = False
    ignored_paths: list[str] = Field(default_factory=list)
    auto_summarize: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConfigUpdateRequest(BaseModel):
    default_model: Optional[str] = None
    default_orchestration: Optional[str] = None
    max_concurrent_agents: Optional[int] = None
    allow_shell_tools: Optional[bool] = None
    ignored_paths: Optional[list[str]] = None
    auto_summarize: Optional[bool] = None
