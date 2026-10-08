"""跨 Session 通信的受信 HTTP 合同；软件身份不属于模型输入。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.schemas.internal_v2.session_context import SessionContextReadRequest
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
    accepted_at: str


class CommunicationObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(min_length=1)
    source_call_id: str = Field(min_length=1)
    page_identity: str = Field(min_length=1)
    page_ordinal: int = Field(ge=0, strict=True)
    expires_at: datetime
    source: GlobalThreadAddress
    target: GlobalThreadAddress
    operation: Literal["read", "wait"]
    query: dict[str, object]

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


class CommunicationGatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["send", "reply", "read", "wait"]
    target: GlobalThreadAddress
    body: dict[str, object]
