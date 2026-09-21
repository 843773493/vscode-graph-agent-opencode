from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

MessageStreamJsonObject = dict[str, JsonValue]

StreamStatus = Literal[
    "open",
    "interrupting",
    "completed",
    "interrupted",
    "failed",
]
ToolExecutionStatus = Literal["running", "completed", "failed"]
ToolExecutionOutcome = Literal[
    "success",
    "provider_error",
    "execution_lost",
    "outcome_unknown",
]
ActivityStatus = Literal[
    "running",
    "waiting",
    "stopping",
    "completed",
    "failed",
    "unknown",
]
ActivityOutcome = Literal[
    "success",
    "user_interrupt",
    "provider_error",
    "execution_lost",
    "outcome_unknown",
]


class _MessageStreamDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageStreamFailureDTO(_MessageStreamDTO):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    after_interrupt_requested: bool | None = None
    resumable: bool | None = None


class MessageStreamBlockSnapshotDTO(_MessageStreamDTO):
    block_id: str = Field(min_length=1)
    block_index: int | None = Field(default=None, ge=0)
    carrier_type: str | None = None
    status: str | None = None
    text: str | None = None
    items: list[MessageStreamJsonObject] = Field(default_factory=list)
    redacted: bool | None = None
    projection: str | None = None
    completion_reason: str | None = None
    partial: bool | None = None
    started_seq: int | None = Field(default=None, ge=0)
    last_event_seq: int | None = Field(default=None, ge=0)
    completed_seq: int | None = Field(default=None, ge=0)
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


class MessageStreamToolExecutionSnapshotDTO(_MessageStreamDTO):
    tool_execution_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: ToolExecutionStatus
    result: str | None = None
    error: str | None = None
    outcome: ToolExecutionOutcome | None = None
    completion_reason: str | None = None
    started_seq: int | None = Field(default=None, ge=0)
    last_event_seq: int | None = Field(default=None, ge=0)
    completed_seq: int | None = Field(default=None, ge=0)
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    tool_invocation_id: str | None = None
    tool_attempt_id: str | None = None


class MessageStreamToolCallDTO(_MessageStreamDTO):
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: MessageStreamJsonObject | None = None
    status: str | None = None
    arguments_complete: bool | None = None
    completion_reason: str | None = None
    started_seq: int | None = Field(default=None, ge=0)
    last_event_seq: int | None = Field(default=None, ge=0)
    completed_seq: int | None = Field(default=None, ge=0)
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    tool_invocation_id: str | None = None
    tool_attempt_id: str | None = None


class MessageStreamModelCallSnapshotDTO(_MessageStreamDTO):
    model_call_id: str = Field(min_length=1)
    attempt: int | None = Field(default=None, ge=0)
    status: str | None = None
    outcome: str | None = None
    completion_reason: str | None = None
    retryable: bool | None = None
    model: str | None = None
    started_seq: int | None = Field(default=None, ge=0)
    last_event_seq: int | None = Field(default=None, ge=0)
    completed_seq: int | None = Field(default=None, ge=0)
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


class MessageStreamActivityDTO(_MessageStreamDTO):
    activity_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    parent_activity_id: str | None = None
    scope_ref: str | None = None
    status: ActivityStatus
    outcome: ActivityOutcome | None = None
    summary: str | None = None
    cancellable: bool | None = None
    resumable: bool | None = None
    side_effect_policy: str | None = None
    resource_refs: list[str] = Field(default_factory=list)
    detail: MessageStreamJsonObject | None = None
    detail_ref: str | None = None
    updated_at: str | None = None
    detail_available: bool | None = None
    detail_error: str | None = None
    started_seq: int | None = Field(default=None, ge=0)
    last_event_seq: int | None = Field(default=None, ge=0)
    completed_seq: int | None = Field(default=None, ge=0)
    started_at: str | None = None
    completed_at: str | None = None


class MessageStreamResourceRefDTO(_MessageStreamDTO):
    resource_id: str = Field(min_length=1)
    lease_id: str | None = None
    operation_id: str | None = None
    status: str | None = None


class MessageStreamActiveStateDTO(_MessageStreamDTO):
    kind: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    carrier_type: str | None = None
    block_id: str | None = None
    tool_call_id: str | None = None
    tool_execution_id: str | None = None
    activity_id: str | None = None
    activity_kind: str | None = None
    status: str = Field(min_length=1)
    last_kind: str | None = None
    last_phase: str | None = None
    reason: str | None = None
    detail_ref: str | None = None
    tool_invocation_id: str | None = None
    tool_attempt_id: str | None = None


class MessageStreamInterruptStateDTO(_MessageStreamDTO):
    request_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    reason: str | None = None
    fact_confirmed: bool | None = None


class MessageStreamRecoveryStateDTO(_MessageStreamDTO):
    status: str = Field(min_length=1)
    code: str | None = None
    message: str | None = None
    resumable: bool | None = None


class MessageStreamSnapshotDTO(_MessageStreamDTO):
    """消息流快照 HTTP 投影；字段与 message_stream.proto 的公共投影一一对应。"""

    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    turn_stream_id: str = Field(min_length=1)
    workspace_id: str | None = Field(default=None, min_length=1)
    snapshot_seq: int = Field(ge=0)
    stream_status: StreamStatus
    agent_loop_status: str = Field(min_length=1)
    current_model_call_id: str | None = None
    current_attempt: int = Field(ge=0)
    blocks: list[MessageStreamBlockSnapshotDTO] = Field(default_factory=list)
    tool_executions: list[MessageStreamToolExecutionSnapshotDTO] = Field(
        default_factory=list
    )
    failure: MessageStreamFailureDTO | None = None
    resumable: bool
    tool_calls: list[MessageStreamToolCallDTO] = Field(default_factory=list)
    model_calls: list[MessageStreamModelCallSnapshotDTO] = Field(
        default_factory=list
    )
    activities: list[MessageStreamActivityDTO] = Field(default_factory=list)
    resource_refs: list[MessageStreamResourceRefDTO] = Field(default_factory=list)
    active_state: MessageStreamActiveStateDTO | None = None
    interrupt_state: MessageStreamInterruptStateDTO | None = None
    recovery: MessageStreamRecoveryStateDTO | None = None
