from __future__ import annotations

from pydantic import BaseModel


class AgentProviderDTO(BaseModel):
    provider_id: str
    model: str
    custom_llm_provider: str
    workspace_default: bool = False
    available: bool = True
    configuration_error: str | None = None


class AgentDTO(BaseModel):
    agent_id: str
    name: str
    description: str | None = None
    model: str
    tools: list[str]
    capabilities: list[str]
    providers: list[AgentProviderDTO]
    workspace_default: bool = False


class WorkspaceDefaultAgentUpdateRequest(BaseModel):
    agent_id: str


class WorkspaceDefaultProviderUpdateRequest(BaseModel):
    provider_id: str
