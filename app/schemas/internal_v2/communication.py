"""跨 Session 通信的受信 HTTP 合同；软件身份不属于模型输入。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.gateway.federation.grants import (
    ContextGrantTarget,
    ForkGrantTarget,
    ResourceNavigationGrantTarget,
)
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextSearchRequest,
)
from app.schemas.internal_v2.session_fork_transfer import SessionForkTransferRequest
from app.services.business.communication.addresses import GlobalThreadAddress
from app.services.business.communication.admission import CommunicationPayload
from app.services.business.communication.wait import DurableDeadline, WaitSelector

_DEADLINE = TypeAdapter(DurableDeadline)
_SELECTOR = TypeAdapter(WaitSelector)


class CommunicationDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: GlobalThreadAddress
    session_id: str = Field(min_length=1)
    workspace_id: str | None = None
    gateway_id: str | None = Field(default=None, min_length=1)


class CommunicationAcceptanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    communication_id: str
    source: GlobalThreadAddress
    target: GlobalThreadAddress
    payload: CommunicationPayload
    request_id: str = Field(min_length=1)


class CommunicationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    communication_id: str
    source: GlobalThreadAddress
    target: GlobalThreadAddress
    payload_hash: str
    admission_id: str
    state: Literal["target_accepted"] = "target_accepted"
    accepted_at: str = Field(description="target 首次持久接受此通信的 UTC 时刻，与 inbox.created_at 同源；重放保留原值，不表示 execution 或 Job 开始。")


class CommunicationObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(min_length=1)
    source_call_id: str = Field(min_length=1)
    page_identity: str = Field(min_length=1)
    page_ordinal: int = Field(ge=0, strict=True)
    expires_at: datetime = Field(
        description="从 tool call 起算的一天恢复窗口；query.deadline 独立冻结真正的 wait 预算"
    )
    source: GlobalThreadAddress
    target: GlobalThreadAddress
    operation: Literal["read", "wait"]
    query: dict[str, object]
    terminal_replay: bool = False

    @model_validator(mode="after")
    def validate_query(self) -> CommunicationObservationRequest:
        if self.expires_at.tzinfo is None:
            raise ValueError("remote observation expires_at 必须包含时区")
        if self.operation == "read":
            if set(self.query) != {"read"}:
                raise ValueError("remote read query 只接受 read")
            SessionContextReadRequest.model_validate(self.query["read"])
        else:
            if self.page_ordinal != 0:
                raise ValueError("remote wait 只有 ordinal=0")
            if set(self.query) != {"selector", "until", "timeout_seconds", "deadline"}:
                raise ValueError("remote wait query 的冻结字段不完整")
            deadline = _DEADLINE.validate_json(json.dumps(self.query["deadline"]), strict=True, extra="forbid")
            timeout = self.query["timeout_seconds"]
            if self.query["until"] not in ("terminal", "state_change") or type(timeout) is not int or timeout != deadline.timeout_seconds:
                raise ValueError("remote wait until/timeout 与冻结 deadline 不一致")
            if self.query["selector"] is not None:
                _SELECTOR.validate_json(json.dumps(self.query["selector"]), strict=True, extra="forbid")
        return self


class ContextQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_gateway_id: str = Field(min_length=1)
    resource_target: ContextGrantTarget
    read: SessionContextReadRequest | None = None
    search: SessionContextSearchRequest | None = None

    @model_validator(mode="after")
    def validate_query(self) -> ContextQueryRequest:
        if (self.read is None) == (self.search is None):
            raise ValueError("上下文查询必须恰好提供 read 或 search")
        query = self.read if self.read is not None else self.search
        if (query.resource_identity, query.uri, query.scope) != (
            self.resource_target.resource_identity, self.resource_target.uri, self.resource_target.scope,
        ):
            raise ValueError("上下文查询与 read grant 资源目标不一致")
        return self


class ResourceNavigationRelayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_gateway_id: str = Field(min_length=1)
    navigation_target: ResourceNavigationGrantTarget
    navigation: ResourceNavigationRequest

    @model_validator(mode="after")
    def require_fixed_uri(self) -> ResourceNavigationRelayRequest:
        if self.navigation.uri != self.navigation_target.uri:
            raise ValueError("资源导航与 read grant 的固定 URI 不一致")
        return self


class CommunicationGatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["send", "reply", "read", "wait", "fork_source", "fork_target"]
    target: GlobalThreadAddress | None = None
    resource_target: ContextGrantTarget | None = None
    fork_target: ForkGrantTarget | None = None
    navigation_target: ResourceNavigationGrantTarget | None = None
    body: dict[str, object]

    @model_validator(mode="after")
    def validate_target(self) -> CommunicationGatewayRequest:
        if sum(target is not None for target in (self.target, self.resource_target, self.fork_target, self.navigation_target)) != 1:
            raise ValueError("relay 必须恰好提供一个 typed target")
        if self.fork_target is not None:
            if self.operation != "fork_" + self.fork_target.fork_role:
                raise ValueError("fork target 必须使用对应的独立 fork capability")
            transfer = SessionForkTransferRequest.model_validate(self.body)
            if transfer.fork_target != self.fork_target:
                raise ValueError("relay fork target 与冻结 envelope 不一致")
        elif self.operation in {"fork_source", "fork_target"}:
            raise ValueError("fork capability 必须绑定 ForkGrantTarget")
        if self.resource_target is not None:
            if self.operation != "read":
                raise ValueError("资源 target 只能使用 read capability")
            query = ContextQueryRequest.model_validate(self.body)
            if query.resource_target != self.resource_target:
                raise ValueError("relay resource_target 与查询 envelope 不一致")
        if self.navigation_target is not None:
            if self.operation != "read":
                raise ValueError("资源导航只允许 read capability")
            query = ResourceNavigationRelayRequest.model_validate(self.body)
            if query.navigation_target != self.navigation_target:
                raise ValueError("relay navigation_target 与查询 envelope 不一致")
        return self
