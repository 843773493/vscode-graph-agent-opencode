from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class RunMode(str, Enum):
    single_agent = "single_agent"
    multi_agent = "multi_agent"


class JobStatus(str, Enum):
    accepted = "accepted"
    queued = "queued"
    running = "running"
    streaming = "streaming"
    waiting_input = "waiting_input"
    paused = "paused"
    interrupt_pending = "interrupt_pending"
    cancelling = "cancelling"
    completed = "completed"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"


class ControlScope(str, Enum):
    job = "job"
    agent = "agent"
    step = "step"


class ControlAction(str, Enum):
    pause = "pause"
    resume = "resume"
    cancel = "cancel"
    skip = "skip"
    replace_instruction = "replace_instruction"
    append_instruction = "append_instruction"
    retry = "retry"


class APIResponse(BaseModel, Generic[T]):
    code: int = Field(default=0)
    message: str = Field(default="ok")
    data: Optional[T] = None
    request_id: Optional[str] = None


class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: Optional[str] = None
    has_more: bool = False


class LogSnapshotResultDTO(BaseModel):
    html_path: str
    meta_path: str


class EntityRef(BaseModel):
    id: str
    name: Optional[str] = None


class TimestampedDTO(BaseModel):
    created_at: datetime
    updated_at: datetime
