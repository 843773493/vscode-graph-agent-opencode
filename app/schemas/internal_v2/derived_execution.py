"""派生执行的有限 HTTP 请求和不含正文的状态回执。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.background_work import BackgroundTaskStatus
from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.schemas.internal_v2.model_reference import ModelReferenceDTO


class DerivedExecutionSourceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(min_length=1, max_length=4096)

    @field_validator("item_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("来源 ID 不得重复")
        return value


class DerivedExecutionSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: CanonicalSessionId
    thread_id: CanonicalThreadId
    idempotency_key: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1)
    source: DerivedExecutionSourceDTO | None = None
    allowed_tool_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(default_factory=list, max_length=128)
    model: ModelReferenceDTO | None = None
    timeout_seconds: float = Field(gt=0, le=300, allow_inf_nan=False)
    max_model_calls: int = Field(ge=1, le=32, strict=True)
    foreground: bool = False

    @field_validator("allowed_tool_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("工具 ID 不得重复")
        return value


class DerivedExecutionScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: CanonicalSessionId
    thread_id: CanonicalThreadId


class DerivedItemReferenceDTO(BaseModel):
    session_id: CanonicalSessionId
    thread_id: CanonicalThreadId
    item_id: str
    content_hash: str


class DerivedDispatchReferenceDTO(BaseModel):
    assembly_id: str
    model_call_id: str
    request_hash: str
    reference_hash: str


class DerivedExecutionResultDTO(BaseModel):
    execution_id: str
    dispatch_ref: DerivedDispatchReferenceDTO
    output_refs: list[DerivedItemReferenceDTO] = Field(max_length=128)
    output_ref_count: int = Field(ge=1)
    output_refs_truncated: bool


class DerivedExecutionDTO(BaseModel):
    task_id: str
    session_id: CanonicalSessionId
    thread_id: CanonicalThreadId
    status: BackgroundTaskStatus
    source_view_id: str
    source_last_ref: DerivedItemReferenceDTO
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None
    result: DerivedExecutionResultDTO | None = None
