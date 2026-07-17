from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .common import TimestampedDTO


TeamMemberSource = Literal["coordinator", "delegated", "attached"]
TeamMemberStatus = Literal["active", "activation_failed", "removed"]
TeamWorkMode = Literal["write", "read_only"]
TeamTaskPhase = Literal["development", "review", "test", "fix", "other"]
TeamTaskStatus = Literal[
    "queued",
    "in_progress",
    "blocked",
    "completed",
    "failed",
    "cancelled",
]


class TeamMemberDTO(BaseModel):
    session_id: str
    role: str
    source: TeamMemberSource
    work_mode: TeamWorkMode
    instructions: str = ""
    status: TeamMemberStatus = "active"
    activation_job_id: str | None = None
    activation_error: str | None = None
    joined_at: datetime
    updated_at: datetime


class TeamTaskDTO(TimestampedDTO):
    task_id: str
    title: str
    description: str
    phase: TeamTaskPhase
    cycle: int = Field(ge=1)
    assignee_session_id: str
    status: TeamTaskStatus
    depends_on_task_ids: list[str] = Field(default_factory=list)
    assigned_job_id: str | None = None
    summary: str | None = None
    error: str | None = None
    updated_by_session_id: str


class TeamEventDTO(BaseModel):
    event_id: str
    team_id: str
    type: str
    actor_session_id: str
    created_at: datetime
    payload: dict[str, object] = Field(default_factory=dict)


class TeamBoardDTO(TimestampedDTO):
    team_id: str
    name: str
    coordinator_session_id: str
    version: int = Field(ge=1)
    members: list[TeamMemberDTO] = Field(default_factory=list)
    tasks: list[TeamTaskDTO] = Field(default_factory=list)
    recent_events: list[TeamEventDTO] = Field(default_factory=list)


class TeamListDTO(BaseModel):
    items: list[TeamBoardDTO]


class TeamMemberOperationDTO(BaseModel):
    board: TeamBoardDTO
    member: TeamMemberDTO
    child_session_id: str | None = None
    child_message_id: str | None = None
    child_job_id: str | None = None


class TeamTaskOperationDTO(BaseModel):
    board: TeamBoardDTO
    task: TeamTaskDTO
    dispatched_job_id: str | None = None
