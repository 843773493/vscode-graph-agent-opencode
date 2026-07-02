from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .common import MessageRole, RunMode, TimestampedDTO


class AttachmentRef(BaseModel):
    file_id: str
    name: Optional[str] = None
    content_type: Optional[str] = None


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


class MessageRunRequest(BaseModel):
    message: MessageCreateRequest
    run: RunOptions


class MessageRunAccepted(BaseModel):
    message_id: str
    job_id: str
    status: str


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
