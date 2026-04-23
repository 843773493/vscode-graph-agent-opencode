from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class SessionCreateRequest(BaseModel):
    title: Optional[str] = "新会话"
    agent_id: Optional[str] = None


class SessionUpdateRequest(BaseModel):
    title: Optional[str] = None
    agent_id: Optional[str] = None


class SessionDTO(BaseModel):
    session_id: str
    workspace_id: str
    title: str
    current_agent_id: str
    created_at: datetime
    updated_at: datetime


class SessionAutoContinueStartRequest(BaseModel):
    poll_interval_seconds: float = 1.0


class SessionAutoContinueStatusDTO(BaseModel):
    session_id: str
    enabled: bool
    task_id: Optional[str]
    task_status: str
    poll_interval_seconds: Optional[float]
    started_at: Optional[datetime]
    forwarded_count: int
    last_forwarded_at: Optional[datetime]
    last_trigger_event_id: Optional[str]
    last_trigger_job_id: Optional[str]
    last_enqueued_job_id: Optional[str]
