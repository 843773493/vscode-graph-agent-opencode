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
