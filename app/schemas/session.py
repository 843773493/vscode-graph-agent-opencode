from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class SessionCreateRequest(BaseModel):
    title: Optional[str] = "新会话"


class SessionUpdateRequest(BaseModel):
    title: Optional[str] = None


class SessionDTO(BaseModel):
    session_id: str
    workspace_id: str
    title: str
    created_at: datetime
    updated_at: datetime
