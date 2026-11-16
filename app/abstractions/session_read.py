"""跨领域共用的 Session 读取与 Thread 解析协议。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable

from app.schemas.internal_v2.session import SessionDTO, SessionListResultDTO


@runtime_checkable
class SessionReaderProtocol(Protocol):
    """按稳定 Session ID 读取 Session DTO。"""

    async def get(self, session_id: str) -> SessionDTO: ...


class SessionListReaderProtocol(Protocol):
    """读取单条会话与分页会话列表，并提供会话读准入。"""

    def session_read_admission(self, session_id: str) -> AbstractAsyncContextManager[None]: ...

    async def get(self, session_id: str) -> SessionDTO: ...

    async def list(
        self,
        workspace_id: str | None = None,
        skip: int = 0,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SessionListResultDTO: ...


@runtime_checkable
class SessionThreadReaderProtocol(Protocol):
    """在会话读准入内读取会话并解析 main／指定 Thread。"""

    def session_read_admission(
        self, session_id: str
    ) -> AbstractAsyncContextManager[None]: ...

    async def get(self, session_id: str) -> SessionDTO: ...

    async def resolve_main_thread(self, session_id: str) -> str: ...

    async def resolve_thread(self, session_id: str, thread_id: str) -> str: ...
