from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class AgentProviderDTO(BaseModel):
    provider_id: str
    model: str
    custom_llm_provider: str
    workspace_default: bool = False


class AgentDTO(BaseModel):
    agent_id: str
    name: str
    description: Optional[str] = None
    model: str
    tools: list[str]
    capabilities: list[str]
    providers: list[AgentProviderDTO]
    workspace_default: bool = False


class WorkspaceDefaultAgentUpdateRequest(BaseModel):
    agent_id: str


class WorkspaceDefaultProviderUpdateRequest(BaseModel):
    provider_id: str
