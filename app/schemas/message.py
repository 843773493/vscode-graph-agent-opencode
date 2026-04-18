from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field

from app.schemas.common import MessageRole, RunMode, JobStatus


class AttachmentRef(BaseModel):
    file_id: str
    name: Optional[str] = None
    content_type: Optional[str] = None


class MessageCreate(BaseModel):
    role: MessageRole = MessageRole.user
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunOptions(BaseModel):
    mode: RunMode = RunMode.single_agent
    agent_id: str
    response_mode: str = "stream"
    async_run: bool = Field(default=True, alias="async")
    max_steps: int = 20
    timeout_seconds: int = 600
    context: dict[str, Any] = Field(default_factory=dict)


class MessageRunRequest(BaseModel):
    message: MessageCreate
    run: RunOptions


class MessageDTO(BaseModel):
    message_id: str
    session_id: str
    role: MessageRole
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class MessageRunAccepted(BaseModel):
    message_id: str
    job_id: str
    status: JobStatus
