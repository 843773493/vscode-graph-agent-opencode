from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CustomToolConfigProtocol(Protocol):
    def get_agent_tool_config(self, agent_id: str) -> dict[str, object]: ...

    def get_llm_provider(self, provider_id: str) -> dict[str, object]: ...
