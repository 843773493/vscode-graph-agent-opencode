"""旧生成器文件的迁移校验；不得用于正常业务请求、存储或调度。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SessionLocatorDTO(BaseModel):
    workspace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class GeneratorTargetDTO(SessionLocatorDTO):
    thread_id: str = Field(min_length=1)


class GeneratorTypeRefDTO(BaseModel):
    type_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class GeneratorTriggerDTO(BaseModel):
    type: Literal["manual", "cron", "interval"] = "manual"
    expression: str | None = None
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str = "UTC"

    @model_validator(mode="after")
    def validate_trigger(self) -> GeneratorTriggerDTO:
        if self.type == "cron" and not self.expression:
            raise ValueError("cron trigger 缺少 expression")
        if self.type == "cron" and self.expression and not croniter.is_valid(self.expression):
            raise ValueError(f"cron expression 非法: {self.expression}")
        if self.type == "interval" and self.interval_seconds is None:
            raise ValueError("interval trigger 缺少 interval_seconds")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"未知时区: {self.timezone}") from error
        return self


class GeneratorPlacementDTO(BaseModel):
    kind: Literal["workspace", "session", "session_folder"]
    workspace_id: str = Field(min_length=1)
    session_id: str | None = None
    folder_id: str | None = None

    @model_validator(mode="after")
    def validate_target(self) -> GeneratorPlacementDTO:
        if self.kind == "session" and self.session_id is None:
            raise ValueError("session placement 缺少 session_id")
        if self.kind == "session_folder" and self.folder_id is None:
            raise ValueError("session_folder placement 缺少 folder_id")
        return self

class GeneratorContextSourceDTO(BaseModel):
    kind: Literal["fresh", "live_session", "snapshot"] = "fresh"
    workspace_id: str | None = None
    session_id: str | None = None
    thread_id: str | None = None
    snapshot_id: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> GeneratorContextSourceDTO:
        if self.kind == "live_session" and (
            self.workspace_id is None or self.session_id is None or not self.thread_id
        ):
            raise ValueError("live_session context source 缺少 workspace_id/session_id/thread_id")
        if self.kind == "snapshot" and self.snapshot_id is None:
            raise ValueError("snapshot context source 缺少 snapshot_id")
        return self


class GeneratorNamingDTO(BaseModel):
    title_template: str = Field(default="{generator.name}", min_length=1, max_length=500)
    path_template: list[str] = Field(
        default_factory=lambda: ["{generator.name}", "{generated_at:yyyy-MM-dd}"],
        max_length=20,
    )


class GeneratorSessionStrategyDTO(BaseModel):
    mode: Literal[
        "new_per_run",
        "continue_existing",
        "fork_new_and_report_back",
    ] = "new_per_run"
    target: GeneratorTargetDTO | None = None
    concurrency: Literal["queue"] = "queue"
    report_back: Literal[
        "none",
        "link",
        "summary",
        "summary_and_link",
        "full",
        "continue_agent",
    ] = "none"

    @model_validator(mode="after")
    def validate_target(self) -> GeneratorSessionStrategyDTO:
        if self.mode == "new_per_run" and self.target is not None:
            raise ValueError("new_per_run strategy 不允许 target")
        if self.mode != "new_per_run" and self.target is None:
            raise ValueError(f"{self.mode} strategy 缺少 target")
        return self


class GeneratorPoliciesDTO(BaseModel):
    overlap: Literal["allow"] = "allow"
    misfire: Literal["skip", "run_latest", "catch_up"] = "skip"
    mount_missing: Literal["pause", "fail"] = "pause"
    delete_outputs: Literal["keep", "cascade"] = "keep"


class GeneratorUIPolicyDTO(BaseModel):
    on_run_started: Literal["stay", "open_generated"] = "stay"
    on_run_completed: Literal["none", "notify", "open_generated", "open_on_failure"] = (
        "notify"
    )


class GeneratorDefinitionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    generator_type: GeneratorTypeRefDTO = Field(
        default_factory=lambda: GeneratorTypeRefDTO(
            type_id="builtin.agent_prompt",
            version="1",
        )
    )
    enabled: bool = True
    trigger: GeneratorTriggerDTO = Field(default_factory=GeneratorTriggerDTO)
    placement: GeneratorPlacementDTO
    execution_workspace_id: str = Field(min_length=1)
    context_source: GeneratorContextSourceDTO = Field(
        default_factory=GeneratorContextSourceDTO
    )
    created_from: SessionLocatorDTO | None = None
    naming: GeneratorNamingDTO = Field(default_factory=GeneratorNamingDTO)
    session_strategy: GeneratorSessionStrategyDTO = Field(
        default_factory=GeneratorSessionStrategyDTO
    )
    policies: GeneratorPoliciesDTO = Field(default_factory=GeneratorPoliciesDTO)
    ui_policy: GeneratorUIPolicyDTO = Field(default_factory=GeneratorUIPolicyDTO)
    config: dict[str, object] = Field(default_factory=dict)

    generator_id: str
    status: Literal["ready", "paused", "blocked"] = "ready"
    status_reason: str | None = None
    revision: int = 1
    created_at: datetime
    updated_at: datetime


GenerationRunStatus = Literal[
    "planned",
    "dispatching",
    "running",
    "reporting",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "skipped",
]


class GenerationOutputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kind: Literal["session"] = "session"
    workspace_id: str
    session_id: str
    title: str | None = None
    navigation_path: list[str] = Field(default_factory=list)


class GenerationRunDTO(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    run_id: str
    generator_id: str
    idempotency_key: str
    status: GenerationRunStatus
    trigger_type: str
    scheduled_for: datetime
    outputs: list[GenerationOutputDTO] = Field(default_factory=list)
    execution_workspace_id: str | None = None
    message_id: str | None = None
    job_id: str | None = None
    report_back_job_id: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
