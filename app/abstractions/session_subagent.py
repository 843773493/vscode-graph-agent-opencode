from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from app.schemas.public_v2.session import SessionDTO


GENERAL_PURPOSE_SUBAGENT = "general-purpose"
BeforeSubagentStart = Callable[[SessionDTO], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SessionSubagentAccepted:
    child_session: SessionDTO
    message_id: str
    job_id: str


@runtime_checkable
class SessionStoreProtocol(Protocol):
    async def get(self, session_id: str) -> SessionDTO: ...

    async def create_delegated(
        self,
        *,
        title: str,
        agent_id: str,
        parent_session_id: str,
        parent_job_id: str,
        parent_tool_call_id: str,
        subagent_type: str,
    ) -> SessionDTO: ...

    async def set_delegation_start_result(
        self,
        session_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> SessionDTO: ...


@runtime_checkable
class SessionSubagentProtocol(Protocol):
    async def delegate(
        self,
        *,
        parent_session_id: str,
        parent_agent_id: str,
        parent_job_id: str,
        parent_tool_call_id: str,
        description: str,
        subagent_type: str,
        title: str | None = None,
        trusted_context: dict[str, object] | None = None,
        before_start: BeforeSubagentStart | None = None,
    ) -> SessionSubagentAccepted: ...
