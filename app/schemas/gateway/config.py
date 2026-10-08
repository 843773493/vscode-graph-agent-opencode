from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config_sources import ConfigSourceLayer


class GatewayConfigReloadStatusDTO(BaseModel):
    available: bool = False
    healthy: bool | None = None
    revision: str | None = None
    restart_required: bool = False
    reason: (
        Literal[
            "invalid_config",
            "restart_required",
            "apply_failed",
            "conflict",
            "rejected",
            "recovery_required",
        ]
        | None
    ) = None
    changed_sections: list[str] = Field(default_factory=list)
    last_error: str | None = None
    error: str | None = None
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


class GatewayConfigPendingHealthProofRequest(BaseModel):
    """新 Gateway generation 返回的脱敏 pending 加载证明。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    config_domain: Literal["gateway"] = "gateway"
    candidate_ref: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    loaded_source: Literal["pending"]
    active_revision: int | None = Field(default=None, ge=0)
    pending_revision: int = Field(ge=0)
    candidate_digest: str = Field(min_length=1)
    effective_digest: str = Field(min_length=1)
    secret_binding_digest: str = Field(min_length=1)
    fencing_token_digest: str = Field(min_length=1)
    health_digest: str = Field(min_length=1)
    gateway_id: str = Field(min_length=1)
    consumer_health_digests: dict[str, str] = Field(default_factory=dict)


class GatewayConfigPendingDiscardRequest(BaseModel):
    """丢弃 Gateway pending 所需的旧 active 安全基线。"""

    model_config = ConfigDict(extra="forbid")

    expected_active_revision: int = Field(ge=0)
    expected_active_digest: str = Field(min_length=1)


class GatewayConfigEventDTO(BaseModel):
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


class GatewayConfigEventsDTO(BaseModel):
    cursor: int
    events: list[GatewayConfigEventDTO] = Field(default_factory=list)
    has_more: bool = False


class GatewayConfigSourceDTO(BaseModel):
    uri: str | None
    layer: ConfigSourceLayer
    precedence: int
    loaded: bool
    source_key: str | None = None
    presence: Literal["present", "absent"] = "present"
    layer_revision: int | None = None
    layer_digest: str | None = None
    source_generation: int | None = None


class GatewayConfigSourcesDTO(BaseModel):
    revision: str
    schema_uri: str | None
    sources: list[GatewayConfigSourceDTO] = Field(default_factory=list)
    policy_manifest: list[dict[str, object]] = Field(default_factory=list)
