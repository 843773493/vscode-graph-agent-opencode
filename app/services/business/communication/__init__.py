"""跨 Session 通信 typed admission/wait 合同层。"""

from __future__ import annotations

from app.services.business.communication.addresses import (
    GlobalThreadAddress,
    ResolvedMainTargetBinding,
)
from app.services.business.communication.admission import (
    CommunicationAdmissionDecision,
    CommunicationKind,
    CommunicationPayload,
    CommunicationSendRequest,
    ExistingOutboxView,
    ReceivedCommunicationView,
    communication_preimage_hash,
    decide_send_admission,
    resolve_request_preimage,
)
from app.services.business.communication.errors import (
    COMMUNICATION_CONTRACT_ERROR_CODES,
    CommunicationContractError,
)
from app.services.business.communication.wait import (
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    MAX_WAIT_TIMEOUT_SECONDS,
    MIN_WAIT_TIMEOUT_SECONDS,
    DurableDeadline,
    WaitClock,
    WaitObservation,
    WaitSelector,
    WaitSelectorKind,
    WaitState,
    WaitTopStatus,
    WaitUntil,
    aggregate_wait_status,
    freeze_wait_deadline,
    remaining_wait_budget_seconds,
    resolve_wait_outcome,
)

__all__ = [
    "COMMUNICATION_CONTRACT_ERROR_CODES",
    "DEFAULT_WAIT_TIMEOUT_SECONDS",
    "MAX_WAIT_TIMEOUT_SECONDS",
    "MIN_WAIT_TIMEOUT_SECONDS",
    "CommunicationAdmissionDecision",
    "CommunicationContractError",
    "CommunicationKind",
    "CommunicationPayload",
    "CommunicationSendRequest",
    "DurableDeadline",
    "ExistingOutboxView",
    "GlobalThreadAddress",
    "ReceivedCommunicationView",
    "ResolvedMainTargetBinding",
    "WaitClock",
    "WaitObservation",
    "WaitSelector",
    "WaitSelectorKind",
    "WaitState",
    "WaitTopStatus",
    "WaitUntil",
    "aggregate_wait_status",
    "communication_preimage_hash",
    "decide_send_admission",
    "freeze_wait_deadline",
    "remaining_wait_budget_seconds",
    "resolve_request_preimage",
    "resolve_wait_outcome",
]
