from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.public_v2.team import (
    TeamBoardDTO,
    TeamListDTO,
    TeamMemberOperationDTO,
    TeamTaskOperationDTO,
    TeamTaskPhase,
    TeamTaskStatus,
    TeamWorkMode,
    TeamEventDTO,
)


@runtime_checkable
class TeamStoreProtocol(Protocol):
    def create(self, board: TeamBoardDTO) -> TeamBoardDTO: ...

    def get(self, team_id: str) -> TeamBoardDTO: ...

    def list(self) -> list[TeamBoardDTO]: ...

    def save(self, board: TeamBoardDTO) -> TeamBoardDTO: ...

    def append_event(self, event: TeamEventDTO) -> None: ...

    def recent_events(self, team_id: str, *, limit: int = 20) -> list[TeamEventDTO]: ...


@runtime_checkable
class TeamCoordinationProtocol(Protocol):
    async def create_team(self, *, requester_session_id: str, name: str) -> TeamBoardDTO: ...

    async def list_teams(self, *, requester_session_id: str) -> TeamListDTO: ...

    async def get_board(
        self,
        *,
        requester_session_id: str,
        team_id: str,
    ) -> TeamBoardDTO: ...

    async def create_member(
        self,
        *,
        requester_session_id: str,
        requester_agent_id: str,
        requester_job_id: str,
        requester_tool_call_id: str,
        team_id: str,
        role: str,
        startup_prompt: str,
        instructions: str,
        work_mode: TeamWorkMode,
    ) -> TeamMemberOperationDTO: ...

    async def attach_session(
        self,
        *,
        requester_session_id: str,
        team_id: str,
        target_session_id: str,
        role: str,
        instructions: str,
        work_mode: TeamWorkMode,
        notify: bool,
    ) -> TeamMemberOperationDTO: ...

    async def assign_task(
        self,
        *,
        requester_session_id: str,
        team_id: str,
        assignee_session_id: str,
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
        team_id: str,
        task_id: str,
        status: TeamTaskStatus,
        summary: str,
    ) -> TeamTaskOperationDTO: ...
