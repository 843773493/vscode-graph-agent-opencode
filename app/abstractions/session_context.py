from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypedDict

from app.abstractions.resource_search import GatewayResourceSearchProtocol
from app.schemas.internal_v2.session_fork_transfer import (
    SessionForkTransferRequest,
    SessionForkTransferResultDTO,
)

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

    from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
    from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef

from app.schemas.gateway.workspaces import GatewayWorkspaceListDTO
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionInformationSnapshotDTO,
    SessionListResultDTO,
)
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.schemas.search_federation import (
    GatewayContextSearchRequest,
    GatewayContextSearchResultDTO,
)


class SessionForkTransportProtocol(Protocol):
    async def fork_transfer(self, request: SessionForkTransferRequest) -> SessionForkTransferResultDTO: ...


class WorkspaceSessionContextAccessError(RuntimeError):
    """模型可通过修正目标标识或提醒用户来处理的跨工作区访问错误。"""


class SessionContextSearchPatternError(ValueError):
    """原上下文查询 owner 在读取前拒绝非法正则。"""

    owner = "session_context_query"
    code = "invalid_regex"
    reason = "invalid_search_pattern"

    def detail(self) -> dict[str, str]:
        return {"owner": self.owner, "code": self.code, "reason": self.reason, "message": str(self)}


class SessionContextCursorError(ValueError):
    """原上下文分页器的输入拒绝；读取、搜索和 relay 共用原原因。"""

    owner = "session_context_cursor"
    code = "invalid_context_cursor"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason

    def detail(self) -> dict[str, str]:
        return {"owner": self.owner, "code": self.code, "reason": self.reason, "message": str(self)}


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


class SessionContextMessageSourceProtocol(Protocol):
    async def get_agent_context_state(
        self,
        session_id: str,
        thread_id: str,
    ) -> AgentContextState: ...


@dataclass(frozen=True, slots=True)
class SessionContextHistoryProjection:
    """同一权威读快照取得的 assembly、历史及资源来源。"""

    assembly: ContextAssemblySnapshot
    messages: list[BaseMessage]
    losses: tuple[str, ...]
    resource_activation: ResourceActivationSnapshotRef | None


class SessionContextAssemblySourceProtocol(Protocol):
    """上下文检查面只消费 Saver 冻结的 assembly 和 history diagnostics。"""

    def list_context_assembly_ids(
        self,
        session_id: str,
        thread_id: str,
        *,
        turn_id: str | None = None,
    ) -> tuple[str, ...]: ...

    def list_context_assemblies(
        self,
        session_id: str,
        thread_id: str,
        *,
        turn_id: str | None = None,
    ) -> tuple[ContextAssemblySnapshot, ...]: ...

    def read_context_assembly_history(
        self,
        session_id: str,
        thread_id: str,
        assembly_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> SessionContextHistoryProjection: ...


class SessionLookupProtocol(Protocol):
    def session_read_admission(self, session_id: str) -> AbstractAsyncContextManager[None]: ...

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
    @property
    def workspace_id(self) -> str: ...

    async def read_context(
        self,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO: ...

    async def search_context(
        self,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO: ...


class WorkspaceSessionContextClientProtocol(Protocol):
    @property
    def resource_search(self) -> GatewayResourceSearchProtocol: ...

    async def read_resource_context(
        self, request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO: ...

    async def search_resource_context(
        self, request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO: ...

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO: ...

    async def read_gateway_context(
        self,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO: ...

    async def search_gateway_context(
        self, request: GatewayContextSearchRequest,
    ) -> GatewayContextSearchResultDTO: ...


class WorkspaceSessionContextTransportProtocol(Protocol):
    async def read_resource_context(
        self, request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO: ...

    async def search_resource_context(
        self, request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO: ...

    async def search_gateway_context(
        self, request: GatewayContextSearchRequest,
    ) -> GatewayContextSearchResultDTO: ...

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO: ...
