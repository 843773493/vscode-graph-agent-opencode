from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable

from app.schemas.internal_v2.session import SessionDTO


@runtime_checkable
class TurnSessionLookupProtocol(Protocol):
    def session_read_admission(
        self, session_id: str
    ) -> AbstractAsyncContextManager[None]: ...

    async def get(self, session_id: str) -> SessionDTO: ...

    async def resolve_main_thread(self, session_id: str) -> str: ...

    async def resolve_thread(self, session_id: str, thread_id: str) -> str: ...
