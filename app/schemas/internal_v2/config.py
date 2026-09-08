from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigDTO(BaseModel):
    default_model: str
    default_orchestration: str
    max_concurrent_agents: int = 4
    allow_shell_tools: bool = False
    ignored_paths: list[str] = Field(default_factory=list)
    auto_summarize: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConfigSourceDTO(BaseModel):
    path: str
    layer: Literal["inline", "user", "user_local", "workspace", "sqlite"]
    precedence: int
    loaded: bool
    source_key: str | None = None
    presence: Literal["present", "absent"] = "present"
    layer_revision: int | None = None
    layer_digest: str | None = None
    source_generation: int | None = None


class ConfigSourcesDTO(BaseModel):
    revision: str
    schema_path: str
    sources: list[ConfigSourceDTO] = Field(default_factory=list)
    runtime_overrides: list[str] = Field(default_factory=list)
    policy_manifest: list[dict[str, object]] = Field(default_factory=list)


class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # 当前 REST 入口只允许写入 Workspace-owned runtime override SQLite layer。
    # 用户级 JSONC 物化需要独立的 source-writer 入口，不能借此接口绕过文件 CAS。
    config_layer: Literal["runtime_override"] = Field(...)
    scope: Literal["workspace"] = Field(...)
    base_layer_revision: int | None = Field(..., ge=1)
    base_layer_digest: str | None = Field(..., min_length=1)
    expected_active_revision: int | None = Field(..., ge=0)
    expected_active_digest: str | None = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1, max_length=200)
    default_model: Optional[str] = None
    default_orchestration: Optional[str] = None
    max_concurrent_agents: Optional[int] = None
    allow_shell_tools: Optional[bool] = None
    ignored_paths: Optional[list[str]] = None
    auto_summarize: Optional[bool] = None

    @model_validator(mode="after")
    def validate_cas_pairs(self) -> "ConfigUpdateRequest":
        if (self.base_layer_revision is None) != (self.base_layer_digest is None):
            raise ValueError(
                "base_layer_revision 和 base_layer_digest 必须同时提供或同时为空"
            )
        if (self.expected_active_revision is None) != (
            self.expected_active_digest is None
        ):
            raise ValueError(
                "expected_active_revision 和 expected_active_digest 必须同时提供或同时为空"
            )
        return self


class ConfigReloadStatusDTO(BaseModel):
    healthy: bool
    revision: str
    restart_required: bool = False
    reason: Literal[
        "invalid_config",
        "restart_required",
        "apply_failed",
        "conflict",
        "rejected",
        "recovery_required",
    ] | None = None
    changed_sections: list[str] = Field(default_factory=list)
    last_success_at: str
    last_attempt_at: str
    last_error: str | None = None
    state: str | None = None
    active_revision: int | None = None
    pending_revision: int | None = None
    candidate_id: str | None = None
    candidate_ref: str | None = None
    attempt_id: str | None = None
    apply_id: str | None = None
    layer_digests: dict[str, str | None] = Field(default_factory=dict)
    applied_paths: list[str] = Field(default_factory=list)
    deferred_paths: list[str] = Field(default_factory=list)


class ConfigStartupContractDTO(BaseModel):
    """Workspace 启动 pending 所需的元数据；不返回候选 payload。"""

    config_domain: Literal["workspace"] = "workspace"
    candidate_ref: str
    candidate_id: str
    pending_revision: int
    candidate_digest: str
    effective_digest: str
    target_generation: str
    fencing_token: str
    secret_binding_digest: str


class ConfigRestartFailureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str = Field(min_length=1, max_length=2000)
    old_runtime_recovered: bool = True


class ConfigPendingHealthProofRequest(BaseModel):
    """新 Workspace generation 返回的脱敏 pending 加载证明。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    config_domain: Literal["workspace"]
    loaded_source: Literal["pending"]
    candidate_id: str = Field(min_length=1)
    loaded_commit_revision: int = Field(ge=0)
    effective_digest: str = Field(min_length=1)
    candidate_digest: str = Field(min_length=1)
    secret_binding_digest: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    fencing_token_digest: str = Field(min_length=1)


class ConfigPendingDiscardRequest(BaseModel):
    """丢弃 pending 所需的旧 active 安全基线。"""

    model_config = ConfigDict(extra="forbid")

    expected_active_revision: int = Field(ge=0)
    expected_active_digest: str = Field(min_length=1)


class ConfigEventDTO(BaseModel):
    event_seq: int
    event_id: str
    config_domain: str
    candidate_id: str | None = None
    attempt_id: str | None = None
    apply_id: str | None = None
    idempotency_key: str | None = None
    commit_revision: int | None = None
    active_revision: int | None = None
    pending_revision: int | None = None
    source: str
    result: Literal[
        "applied",
        "restart_required",
        "restart_failed",
        "apply_failed",
        "rejected",
        "conflict",
        "discarded",
        "recovery_required",
        "unchanged",
    ]
    activation_scope: Literal[
        "current",
        "next_job",
        "next_session",
        "restart_workspace",
        "restart_gateway",
        "mixed",
        "unknown",
    ] = "unknown"
    changed_paths: list[str] = Field(default_factory=list)
    applied_paths: list[str] = Field(default_factory=list)
    deferred_paths: list[str] = Field(default_factory=list)
    error: str | None = None
    occurred_at: str


class ConfigEventsDTO(BaseModel):
    cursor: int
    events: list[ConfigEventDTO] = Field(default_factory=list)
    has_more: bool = False
