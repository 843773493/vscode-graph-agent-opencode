from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .model_reference import ModelReferenceDTO


class AgentModelDTO(BaseModel):
    model_ref: ModelReferenceDTO
    model: str
    name: str | None = None
    provider_name: str | None = None
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
    models: list[AgentModelDTO]
    workspace_default: bool = False


class WorkspaceDefaultAgentUpdateRequest(BaseModel):
    agent_id: str


class WorkspaceDefaultModelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelReferenceDTO
