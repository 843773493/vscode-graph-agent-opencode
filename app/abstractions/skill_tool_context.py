from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SkillToolConfigProtocol(Protocol):
    def get_agent_tool_config(self, agent_id: str) -> dict[str, object]: ...


@runtime_checkable
class SkillToolMessageProtocol(Protocol):
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
class SkillToolSessionProtocol(Protocol):
    async def get(self, session_id: str) -> object: ...
