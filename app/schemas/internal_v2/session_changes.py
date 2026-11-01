from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer

from app.core.resource_uri import ResourceUriGrammarError, parse_resource_uri

SessionChangesetKind = Literal["all", "turn"]
SessionFileChangeKind = Literal["create", "edit", "delete"]


def validate_change_file_reference(value: str, *, local_gateway_id: str | None = None) -> str:
    try:
        resource = parse_resource_uri(value, local_gateway_id=local_gateway_id)
    except ResourceUriGrammarError as error:
        raise ValueError(f"文件变更 URI 校验失败: reason={error.reason_code}") from error
    if resource.kind not in {"files", "attachments"} or len(resource.tail_segments) < 2:
        raise ValueError("文件变更只能引用明确的普通文件或附件目录 child URI")
    return resource.display_uri


SessionChangeFileURI = Annotated[
    str, AfterValidator(validate_change_file_reference), PlainSerializer(validate_change_file_reference),
]


class SessionChangesSummaryDTO(BaseModel):
    files: int = 0
    additions: int = 0
    deletions: int = 0


class SessionChangesetListItemDTO(BaseModel):
    changeset_id: str
    label: str
    description: str | None = None
    change_kind: SessionChangesetKind
    is_default: bool = False
    turn_id: str | None = None
    summary: SessionChangesSummaryDTO = Field(default_factory=SessionChangesSummaryDTO)


class SessionChangesetListDTO(BaseModel):
    session_id: str
    items: list[SessionChangesetListItemDTO]


class SessionFileChangeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: SessionChangeFileURI
    kind: SessionFileChangeKind
    additions: int = 0
    deletions: int = 0
    reviewed: bool = False
    latest_edit_id: str
    # 保留 ToolMessage 携带的 Provider 原始 call ID；不是 ModelCall 作用域 canonical ID，也不是执行或重试 ID。
    tool_call_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
    turn_ids: list[str] = Field(default_factory=list)
    diff_text: str
    before_preview: str | None = None
    after_preview: str | None = None


class SessionChangesetDTO(BaseModel):
    session_id: str
    changeset_id: str
    label: str
    description: str | None = None
    change_kind: SessionChangesetKind
    turn_id: str | None = None
    status: Literal["ready"] = "ready"
    summary: SessionChangesSummaryDTO = Field(default_factory=SessionChangesSummaryDTO)
    files: list[SessionFileChangeDTO] = Field(default_factory=list)
    generated_at: datetime


class SessionFileReviewRequest(BaseModel):
    file_path: str
    reviewed: bool


class SessionFileReviewResultDTO(BaseModel):
    session_id: str
    file_path: SessionChangeFileURI
    reviewed: bool
