from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable


class AgentContextState(TypedDict):
    records: list[dict[str, object]]
    checkpoint_id: str
    raw_message_count: int
    compacted: bool
    compaction_cutoff: int | None
    history_file_path: str | None


@runtime_checkable
class CustomToolConfigProtocol(Protocol):
    def get_agent_tool_config(self, agent_id: str) -> dict[str, object]: ...

    def get_llm_provider(self, provider_id: str) -> dict[str, object]: ...


@runtime_checkable
class CustomToolMessageProtocol(Protocol):
    async def get_agent_context_state(
        self,
        session_id: str,
    ) -> AgentContextState: ...

    async def list_agent_state_records(
        self,
        session_id: str,
        *,
        strict: bool = False,
    ) -> list[dict[str, object]]: ...

    def append_system_reminder(
        self,
        *,
        session_id: str,
        reminder: str,
        response_metadata: dict[str, object],
        checkpoint_source: str,
        assistant_text: str = "",
        assistant_response_metadata: dict[str, object] | None = None,
    ) -> bool: ...


@runtime_checkable
class CustomToolSessionProtocol(Protocol):
    async def get(self, session_id: str) -> object: ...
