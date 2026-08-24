from __future__ import annotations

from typing import Protocol, TypedDict

from app.schemas.gateway import GatewayWorkspaceListDTO
from app.schemas.public_v2.session import (
    SessionDTO,
    SessionInformationSnapshotDTO,
    SessionListResultDTO,
)
from app.schemas.public_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)


class WorkspaceSessionContextAccessError(RuntimeError):
    """模型可通过修正目标标识或提醒用户来处理的跨工作区访问错误。"""


class SessionContextRevisionChangedError(RuntimeError):
    """后续读取所绑定的上下文 revision 已发生变化。"""

    def __init__(self, *, expected_revision: str, actual_revision: str) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "session context revision changed: "
            f"expected={expected_revision}, actual={actual_revision}; "
            "请重新执行 read_context 或 search_context 获取新 cursor"
        )


class AgentContextState(TypedDict):
    records: list[dict[str, object]]
    checkpoint_id: str
    raw_message_count: int
    compacted: bool
    compaction_cutoff: int | None
    history_file_path: str | None


class SessionContextMessageSourceProtocol(Protocol):
    async def get_agent_context_state(self, session_id: str) -> AgentContextState: ...


class SessionLookupProtocol(Protocol):
    async def get(self, session_id: str) -> SessionDTO: ...

    async def list(
        self,
        workspace_id: str | None = None,
        skip: int = 0,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SessionListResultDTO: ...


class SessionInformationSourceProtocol(Protocol):
    async def get_information(
        self,
        session_id: str,
    ) -> SessionInformationSnapshotDTO: ...


class SessionContextQueryProtocol(Protocol):
    async def read_context(
        self,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO: ...

    async def search_context(
        self,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO: ...


class WorkspaceSessionContextClientProtocol(Protocol):
    async def read_gateway_context(
        self,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO: ...

    async def search_gateway_context(
        self,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO: ...

    async def read_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO: ...

    async def search_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO: ...


class WorkspaceSessionContextTransportProtocol(Protocol):
    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO: ...

    async def read_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO: ...

    async def search_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO: ...
