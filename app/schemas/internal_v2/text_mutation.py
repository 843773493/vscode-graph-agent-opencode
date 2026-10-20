"""与 workspace/v2/public.proto 一致的条件文本修改边界。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.resource_uri import parse_resource_uri

Version = Annotated[str, Field(pattern=r"^(absent|sha256:[0-9a-f]{64})$")]
MAX_MUTATION_REQUEST_BYTES = 1048576
MAX_MUTATION_RESPONSE_BYTES = 8388608


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreateText(_Model):
    target: str = Field(min_length=1)
    content: str
    expected_version: Version


class EditText(_Model):
    target: str = Field(min_length=1)
    old_string: str
    new_string: str
    expected_version: Version
    replace_all: bool = False


class PatchText(_Model):
    target: str = Field(min_length=1)
    input: str
    expected_versions: dict[str, Version]
    explanation: str


TextMutation = CreateText | EditText | PatchText


class TextMutationRequest(_Model):
    create: CreateText | None = None
    edit: EditText | None = None
    patch: PatchText | None = None
    timeout_ms: int = Field(default=8000, ge=1, le=30000)
    max_response_bytes: int = Field(default=MAX_MUTATION_RESPONSE_BYTES, ge=1024, le=MAX_MUTATION_RESPONSE_BYTES)

    @model_validator(mode="after")
    def require_one_command(self) -> TextMutationRequest:
        if sum(item is not None for item in (self.create, self.edit, self.patch)) != 1:
            raise ValueError("文本修改必须恰好提供 create/edit/patch 之一")
        # 此封套属于跨进程传输；原生路径由普通工具在其明确目标 owner 解释。
        parsed = parse_resource_uri(self.command.target)
        if parsed.scope != "workspace" or parsed.kind != "files":
            raise ValueError("文本传输只接受固定 Workspace files URI")
        return self

    @property
    def command(self) -> TextMutation:
        if self.create is not None:
            return self.create
        if self.edit is not None:
            return self.edit
        assert self.patch is not None
        return self.patch

    @classmethod
    def from_command(cls, command: TextMutation, **limits: int) -> TextMutationRequest:
        field = "create" if isinstance(command, CreateText) else "edit" if isinstance(command, EditText) else "patch"
        return cls(**{field: command}, **limits)


class TextMutationFileDTO(_Model):
    path: str
    operation: Literal["add", "update", "delete", "move"] | None = None
    state: Literal["published", "not_published", "not_attempted", "unknown"]
    expected_version: Version
    intended_version: Version | None = None
    exists: bool | None = None
    content_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    content_observation_error: str | None = None


class TextMutationReceiptDTO(_Model):
    status: Literal["success", "error"]
    effect: Literal["completed", "partial", "not_executed", "unknown"]
    target: str | None = None
    journal_id: str | None = None
    files: tuple[TextMutationFileDTO, ...] = ()
    retryable: Literal[False] = False
    explanation: str = ""
    fuzz: int | None = None
    owner: str | None = None
    code: str | None = None
    error_type: str | None = None
    error: str | None = None
    recovery: str | None = None
    lock_release_error: str | None = None
    journal_error: str | None = None


class TextMutationSnapshotDTO(_Model):
    uri: str
    before_exists: bool
    before_content: str
    after_exists: bool | None = None
    after_content: str | None = None
    after_error: str | None = None

    @model_validator(mode="after")
    def require_observation(self) -> TextMutationSnapshotDTO:
        if self.after_error is None and (self.after_exists is None or self.after_content is None):
            raise ValueError("文本修改快照缺少可核后件或明确观察错误")
        return self


class TextMutationTransferDTO(_Model):
    receipt: TextMutationReceiptDTO
    snapshots: tuple[TextMutationSnapshotDTO, ...] = ()
