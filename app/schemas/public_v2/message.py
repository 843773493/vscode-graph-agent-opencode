from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .attachment import AttachmentRef
from .common import MessageRole, RunMode, TimestampedDTO
from .job import (
    JobDispatchSnapshotDTO,
    JobDispatchStatus,
)
from .pending_request import PendingRequestKind


class MessageCreateRequest(BaseModel):
    role: MessageRole = MessageRole.user
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class RunOptions(BaseModel):
    mode: RunMode = RunMode.single_agent
    agent_id: Optional[str] = None
    response_mode: str = "stream"
    async_run: bool = Field(default=True, alias="async")
    max_steps: int = 20
    timeout_seconds: int = 600
    context: dict[str, object] = Field(default_factory=dict)
    queue: PendingRequestKind | None = None


class MessageRunRequest(BaseModel):
    message: MessageCreateRequest
    run: RunOptions


class MessageRunAccepted(BaseModel):
    message_id: str
    job_id: str
    status: JobDispatchStatus
    dispatch: JobDispatchSnapshotDTO


TurnReplayAction = Literal["retry_failed", "regenerate", "edit_and_continue"]


class MessageReplayRequest(BaseModel):
    action: TurnReplayAction
    content: str | None = None
    acknowledge_context_only: bool = False


class MessageReplayAccepted(MessageRunAccepted):
    session_id: str
    action: TurnReplayAction
    replaced_message_id: str
    removed_message_count: int
    workspace_changes_reverted: Literal[False] = False
    notice: str


class MessageDTO(TimestampedDTO):
    message_id: str
    session_id: str
    role: MessageRole
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class AgentStateMessagesDTO(BaseModel):
    session_id: str
    message_count: int
    jsonl: str
