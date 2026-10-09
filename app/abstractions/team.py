from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.internal_v2.team import (
    TeamBoardDTO,
    TeamEventDTO,
    TeamListDTO,
    TeamMemberOperationDTO,
    TeamTaskOperationDTO,
    TeamTaskPhase,
    TeamTaskStatus,
    TeamWorkMode,
)


@runtime_checkable
class TeamStoreProtocol(Protocol):
    async def commit(self, board: TeamBoardDTO, event: TeamEventDTO, *, wakeup: dict[str, object] | None = None) -> int: ...

    def get(self, session_id: str, team_id: str) -> TeamBoardDTO: ...

    def list(self, session_id: str) -> list[TeamBoardDTO]: ...

    def recent_events(self, session_id: str, team_id: str, *, limit: int = 20) -> list[TeamEventDTO]: ...

    def latest_task_event(self, session_id: str, team_id: str, task_id: str) -> TeamEventDTO | None: ...


class TeamFanoutProtocol(Protocol):
    async def reconcile(self, session_id: str) -> None: ...

    async def dispatch(self, session_id: str, revision: int) -> str | None: ...


@runtime_checkable
class TeamCoordinationProtocol(Protocol):
    async def create_team(self, *, requester_session_id: str, requester_thread_id: str, name: str) -> TeamBoardDTO: ...

    async def list_teams(self, *, requester_session_id: str, requester_thread_id: str) -> TeamListDTO: ...

    async def get_board(
        self,
        *,
        requester_session_id: str,
        requester_thread_id: str,
        team_id: str,
    ) -> TeamBoardDTO: ...

    async def create_member(
        self,
        *,
        requester_session_id: str,
        requester_thread_id: str,
        requester_agent_id: str,
        requester_job_id: str,
        requester_tool_call_id: str,
        team_id: str,
        role: str,
        startup_prompt: str,
        instructions: str,
        work_mode: TeamWorkMode,
    ) -> TeamMemberOperationDTO: ...

    async def assign_task(
        self,
        *,
        requester_session_id: str,
        requester_thread_id: str,
        team_id: str,
        assignee_thread_id: str,
        title: str,
        description: str,
        phase: TeamTaskPhase,
        cycle: int,
        depends_on_task_ids: list[str],
        start_assignee: bool,
    ) -> TeamTaskOperationDTO: ...

    async def update_task(
        self,
        *,
        requester_session_id: str,
        requester_thread_id: str,
        team_id: str,
        task_id: str,
        status: TeamTaskStatus,
        summary: str,
    ) -> TeamTaskOperationDTO: ...
