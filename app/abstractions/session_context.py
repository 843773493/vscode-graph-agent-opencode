from __future__ import annotations

from typing import Protocol, TypedDict

from app.schemas.public_v2.session_context import (
    SessionContextGrepResultDTO,
    SessionContextReadResultDTO,
    SessionRecentTextMessagesDTO,
)


class WorkspaceSessionContextAccessError(RuntimeError):
    """模型可通过修正目标标识或提醒用户来处理的跨工作区访问错误。"""


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
    async def get(self, session_id: str) -> object: ...


class SessionContextQueryProtocol(Protocol):
    async def recent_text(
        self,
        session_id: str,
        *,
        rounds: int = 5,
    ) -> SessionRecentTextMessagesDTO: ...

    async def grep(
        self,
        session_id: str,
        *,
        pattern: str,
        case_sensitive: bool = False,
        max_matches: int = 20,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextGrepResultDTO: ...

    async def read_lines(
        self,
        session_id: str,
        *,
        line_start: int = 1,
        line_count: int = 20,
        max_chars_per_line: int = 4000,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextReadResultDTO: ...


class WorkspaceSessionContextClientProtocol(Protocol):
    async def recent_text_in_workspace(
        self,
        workspace_id: str,
        session_id: str,
        *,
        rounds: int = 5,
    ) -> SessionRecentTextMessagesDTO: ...

    async def grep_in_workspace(
        self,
        workspace_id: str,
        session_id: str,
        *,
        pattern: str,
        case_sensitive: bool = False,
        max_matches: int = 20,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextGrepResultDTO: ...

    async def read_lines_in_workspace(
        self,
        workspace_id: str,
        session_id: str,
        *,
        line_start: int = 1,
        line_count: int = 20,
        max_chars_per_line: int = 4000,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextReadResultDTO: ...
