from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.canonical_params import CanonicalSessionId

LLMRequestLogLimit = Literal["result_limit", "scan_limit", "byte_limit", "deadline", "item_size_limit"]


class LLMRequestLogRecordDTO(BaseModel):
    session_id: str
    job_id: str | None = None
    timestamp: int = Field(description="handler 结束或失败后组装日志的 epoch 毫秒时刻；文件名的 epoch 纳秒只用于排序。")
    file_name: str
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    upstream: dict[str, Any] = Field(default_factory=dict)


class LLMRequestLogReferenceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: CanonicalSessionId
    file_name: str = Field(pattern=r"^[0-9]+\.json$")
    content_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_codepoint: int = Field(ge=0)
    end_codepoint: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> LLMRequestLogReferenceDTO:
        if self.end_codepoint <= self.start_codepoint:
            raise ValueError("请求日志位置必须是非空码点半开区间")
        return self


class LLMRequestLogMatchDTO(BaseModel):
    reference: LLMRequestLogReferenceDTO
    timestamp: int
    preview: str
    preview_start_codepoint: int = Field(ge=0)


class LLMRequestLogLocationDTO(BaseModel):
    reference: LLMRequestLogReferenceDTO
    record: LLMRequestLogRecordDTO
    text: str = Field(description="request、response、upstream 的固定顺序、两空格缩进 JSON；位置采用 Unicode 码点")


class WorkspaceLLMRequestLogMatchDTO(BaseModel):
    session_uri: str
    session_title: str
    match: LLMRequestLogMatchDTO


class WorkspaceLLMRequestLogSearchResultsDTO(BaseModel):
    catalog_revision: int
    items: list[WorkspaceLLMRequestLogMatchDTO]
    cursor: str | None = None
    complete: bool
    limited_reason: LLMRequestLogLimit | None = None
    scanned_nodes: int = Field(ge=0)
    scanned_records: int = Field(ge=0)
    scanned_bytes: int = Field(ge=0)
