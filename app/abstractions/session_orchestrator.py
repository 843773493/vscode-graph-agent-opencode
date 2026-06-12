from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SessionOrchestratorProtocol(Protocol):
    async def create_and_run(self, session_id: str, content: str) -> str: ...
