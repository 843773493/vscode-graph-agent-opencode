from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LLMRequestLogRecordDTO(BaseModel):
    session_id: str
    job_id: str | None = None
    timestamp: int = Field(description="handler 结束或失败后组装日志的 epoch 毫秒时刻；文件名的 epoch 纳秒只用于排序。")
    file_name: str
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    upstream: dict[str, Any] = Field(default_factory=dict)
