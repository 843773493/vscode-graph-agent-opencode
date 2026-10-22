from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId


class WorkspaceObservationSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str = Field(min_length=1)
    kind: Literal["session_activity", "session_trace", "workspace_files"]
    session_id: CanonicalSessionId | None = None
    thread_id: CanonicalThreadId | None = None
    activity_after: int | None = Field(default=None, ge=0)
    trace_after: str | None = Field(default=None, min_length=1)
    paths: list[str] = Field(default_factory=list)
    files_after: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.kind == "session_trace":
            if self.session_id is None:
                raise ValueError("Trace 观察者必须指定 session_id")
        elif self.session_id is not None or self.trace_after is not None or self.thread_id is not None:
            raise ValueError("仅 Trace 观察者可以指定 session_id/thread_id/trace_after")
        if self.kind != "session_activity" and self.activity_after is not None:
            raise ValueError("仅会话活动观察者可以指定 activity_after")
        if self.kind != "workspace_files" and (self.paths or self.files_after is not None):
            raise ValueError("仅文件观察者可以指定 paths/files_after")
        return self


class WorkspaceObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscriptions: list[WorkspaceObservationSubscription] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        ids = [item.subscription_id for item in self.subscriptions]
        if len(set(ids)) != len(ids):
            raise ValueError("Workspace 观察者 subscription_id 不得重复")
        return self


class WorkspaceObservationFrameDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str = Field(min_length=1)
    state: Literal["connected", "frame", "error", "closed"]
    frame: str | None = None
    status: int | None = Field(default=None, ge=400, le=599)
    detail_json: str | None = None
    # connected 时逻辑 reader 的续读水位；files 是事件序号十进制字符串，
    # Trace/activity 当前为 None，与历史分页 cursor、路由 revision 无关。
    cursor: str | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state == "frame" and not self.frame:
            raise ValueError("观察者 frame 信封缺少原 SSE 帧")
        if self.state == "error" and (self.status is None or self.detail_json is None):
            raise ValueError("观察者 error 信封缺少 status/detail_json")
        return self
