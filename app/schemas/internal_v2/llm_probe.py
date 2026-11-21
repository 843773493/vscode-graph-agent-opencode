"""一次性模型探测输入与非秘密观察；不承担配置保存或执行历史。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.schemas.internal_v2.config import ConfigSourceBaselineDTO
from app.schemas.internal_v2.model_reference import ModelReferenceDTO


class LlmCredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    scope: Literal["provider", "model"]
    provider_id: str = Field(min_length=1, max_length=64)
    model_id: str | None = Field(default=None, min_length=1, max_length=64)
    value: SecretStr = Field(min_length=1, max_length=65536)

    @model_validator(mode="after")
    def check_scope(self) -> LlmCredentialInput:
        if (self.scope == "model") != (self.model_id is not None):
            raise ValueError("model 范围必须提供 model_id；provider 范围禁止 model_id")
        return self


class LlmDiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    gateway_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    mode: Literal["active", "draft"]
    provider_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    edit_version: int = Field(ge=0)
    source_key: str | None = None
    jsonc: str | None = Field(default=None, max_length=2 * 1024 * 1024)
    expected_source_baseline: dict[str, ConfigSourceBaselineDTO] = Field(default_factory=dict)
    expected_active_revision: int | None = Field(default=None, ge=0)
    expected_active_digest: str | None = None
    credential_input: LlmCredentialInput | None = None

    @model_validator(mode="after")
    def check_mode(self) -> LlmDiscoverRequest:
        if self.mode == "active":
            if self.source_key is not None or self.jsonc is not None or self.expected_source_baseline or self.credential_input is not None:
                raise ValueError("active 探测不能混入草稿、来源基线或临时凭据")
            if self.expected_active_revision is None or not self.expected_active_digest:
                raise ValueError("active 探测必须提供 active revision 和 digest")
        else:
            if not self.source_key or self.jsonc is None or not self.expected_source_baseline:
                raise ValueError("draft 探测必须提供来源、JSONC 草稿及参与来源基线")
            if self.expected_active_revision is not None or self.expected_active_digest is not None:
                raise ValueError("draft 探测使用来源基线，不能混入 active 版本")
        return self


class LlmCredentialObservationDTO(BaseModel):
    source: Literal["none", "vault", "environment", "temporary", "oauth"]
    secret_id: str | None = None
    revision: int | None = None
    scope: Literal["provider", "model"] | None = None


class LlmProbeObservationDTO(BaseModel):
    gateway_id: str
    workspace_id: str
    mode: Literal["active", "draft"]
    provider_id: str
    edit_version: int
    source_key: str | None = None
    source_baseline: dict[str, ConfigSourceBaselineDTO] = Field(default_factory=dict)
    active_revision: int | None = None
    active_digest: str | None = None
    credential: LlmCredentialObservationDTO
    protocol: str
    endpoint: str
    observed_at: str


class LlmDiscoveredModelDTO(BaseModel):
    model: str
    name: str | None = None


class LlmProbeErrorDTO(BaseModel):
    category: str
    message: str
    status_code: int | None = None
    field: str | None = None


class LlmDiscoverResultDTO(BaseModel):
    observation: LlmProbeObservationDTO
    source: Literal["upstream"] = "upstream"
    models: list[LlmDiscoveredModelDTO]
    complete: bool
    error: LlmProbeErrorDTO | None = None


class LlmTestRequest(LlmDiscoverRequest):
    model_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    streaming: bool | None = None
    timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)


class LlmTestStartDTO(BaseModel):
    request_id: str
    observation: LlmProbeObservationDTO
    model_ref: ModelReferenceDTO
    streaming: bool


class LlmTestDeltaDTO(BaseModel):
    request_id: str
    text: str


class LlmTestResultDTO(LlmTestStartDTO):
    status: Literal["passed", "failed", "cancelled"]
    first_content_ms: float | None
    elapsed_ms: float
    preview: str
    preview_truncated: bool
    usage: dict[str, object] | None
    error: LlmProbeErrorDTO | None = None
