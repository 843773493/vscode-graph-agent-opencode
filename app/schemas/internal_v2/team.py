from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .common import TimestampedDTO

TeamMemberSource = Literal["coordinator", "delegated"]
TeamMemberStatus = Literal["active", "pending", "cancelled"]
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
    thread_id: str
    delegation_id: str | None = None
    role: str
    source: TeamMemberSource
    work_mode: TeamWorkMode
    instructions: str = ""
    status: TeamMemberStatus = "active"
    joined_at: datetime
    updated_at: datetime


class TeamTaskDTO(TimestampedDTO):
    task_id: str
    title: str
    description: str
    phase: TeamTaskPhase
    cycle: int = Field(ge=1)
    assignee_thread_id: str
    status: TeamTaskStatus
    depends_on_task_ids: list[str] = Field(default_factory=list)
    assigned_job_id: str | None = None
    summary: str | None = None
    error: str | None = None
    updated_by_thread_id: str


class TeamEventDTO(BaseModel):
    event_id: str
    team_id: str
    type: str
    session_id: str
    actor_thread_id: str
    created_at: datetime
    payload: dict[str, object] = Field(default_factory=dict)


class TeamBoardDTO(TimestampedDTO):
    team_id: str
    name: str
    session_id: str
    coordinator_thread_id: str
    version: int = Field(ge=1)
    members: list[TeamMemberDTO] = Field(default_factory=list)
    tasks: list[TeamTaskDTO] = Field(default_factory=list)
    recent_events: list[TeamEventDTO] = Field(default_factory=list)


class TeamListDTO(BaseModel):
    items: list[TeamBoardDTO]


class TeamMemberOperationDTO(BaseModel):
    board: TeamBoardDTO
    member: TeamMemberDTO
    child_thread_id: str | None = None
    child_delegation_id: str | None = None
    child_admission_state: str | None = None


class TeamTaskOperationDTO(BaseModel):
    board: TeamBoardDTO
    task: TeamTaskDTO
    dispatched_job_id: str | None = None
