from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.internal_v2.model_reference import ModelReferenceDTO


@runtime_checkable
class CustomToolConfigProtocol(Protocol):
    def get_agent_tool_config(self, agent_id: str) -> dict[str, object]: ...

    def get_llm_model(self, model: ModelReferenceDTO) -> dict[str, object]: ...

    async def resolve_llm_model(self, model: ModelReferenceDTO) -> dict[str, object]: ...
