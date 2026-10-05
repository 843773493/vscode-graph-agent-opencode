"""领域 owner 共用的真实 Thread 准入与外部传输事实边界。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from app.schemas.internal_v2.session import SessionDTO

RetentionScope = Literal["thread", "workspace"]
ResourceKind = Literal["terminal", "browser"]
ResourceSnapshot = dict[str, object]


class ResourceSessionReader(Protocol):
    @property
    def workspace_id(self) -> str: ...

    def session_write_admission(self, session_id: str) -> AbstractAsyncContextManager[None]: ...

    async def get(self, session_id: str) -> SessionDTO: ...


class ResourceThreadResolver(Protocol):
    def main_thread_id(self, session_id: str) -> str: ...

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path: ...


class ResourceTransport(Protocol):
    async def get_resource(self, resource_id: str) -> ResourceSnapshot: ...

    async def list_resources(self) -> list[ResourceSnapshot]: ...

    async def stop_resource(self, resource_id: str, *, reason: str | None = None) -> ResourceSnapshot: ...

    async def delete_resource(self, resource_id: str) -> ResourceSnapshot: ...

    async def sync_owner_revision(self, resource_id: str, owner_revision: int) -> None: ...

    async def issue_transport_grant(self, resource_id: str, *, owner_revision: int, association: str | None) -> ResourceSnapshot: ...
