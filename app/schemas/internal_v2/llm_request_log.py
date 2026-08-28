from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LLMRequestLogRecordDTO(BaseModel):
    session_id: str
    job_id: str | None = None
    timestamp: int
    file_name: str
    file_path: str
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    upstream: dict[str, Any] = Field(default_factory=dict)
