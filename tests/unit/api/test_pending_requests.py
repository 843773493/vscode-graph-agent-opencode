from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.messages import create_message_and_run, update_pending_request_policy
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import MessageRunRequest
from app.schemas.internal_v2.pending_request import (
    PendingRequestListDTO,
    PendingRequestPolicyUpdateRequest,
)


class _JobService:
    def __init__(self, *, error: RuntimeError | None = None) -> None:
        self.calls: list[tuple[str, str, str, int | None]] = []
        self.error = error

    async def update_pending_policy(
        self,
        session_id: str,
        message_id: str,
        *,
        delivery_policy: str,
        expected_snapshot_version: int | None,
    ) -> PendingRequestListDTO:
        if self.error is not None:
            raise self.error
        self.calls.append(
            (session_id, message_id, delivery_policy, expected_snapshot_version)
        )
        return PendingRequestListDTO(
            session_id=session_id,
            snapshot_version=4,
        )


class _SessionOrchestrator:
    def __init__(self) -> None:
        self.payload: MessageRunRequest | None = None

    async def create_message(
        self,
        session_id: str,
        payload: MessageRunRequest,
    ) -> dict[str, str]:
        del session_id
        self.payload = payload
        return {"status": JobStatus.queued.value}


@pytest.mark.asyncio
async def test_policy_api_forwards_only_policy_and_snapshot_version() -> None:
    service = _JobService()
    response = await update_pending_request_policy(
        "session_1",
        "message_1",
        PendingRequestPolicyUpdateRequest(
            delivery_policy="after_tool_result",
            expected_snapshot_version=3,
        ),
        _="local",
        request_id="request_1",
        job_service=service,
    )

    assert response.data.snapshot_version == 4
    assert service.calls == [("session_1", "message_1", "after_tool_result", 3)]


@pytest.mark.asyncio
async def test_policy_api_maps_stale_snapshot_to_conflict() -> None:
    service = _JobService(error=RuntimeError("队列快照已过期"))

    with pytest.raises(HTTPException) as raised:
        await update_pending_request_policy(
            "session_1",
            "message_1",
            PendingRequestPolicyUpdateRequest(delivery_policy="after_turn"),
            _="local",
            request_id="request_1",
            job_service=service,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == "队列快照已过期"


@pytest.mark.asyncio
async def test_message_api_forwards_delivery_policy() -> None:
    orchestrator = _SessionOrchestrator()
    payload = MessageRunRequest.model_validate(
        {
            "message": {"content": "按工具结果投递"},
            "run": {
                "mode": "single_agent",
                "agent_id": "default",
                "delivery_policy": "after_tool_result",
            },
        }
    )

    response = await create_message_and_run(
        "session_1",
        payload,
        _="local",
        request_id="request_1",
        message_service=object(),
        session_orchestrator=orchestrator,
    )

    assert response.data == {"status": "queued"}
    assert orchestrator.payload is payload
    assert payload.run.delivery_policy == "after_tool_result"


def test_old_dispatch_fields_are_rejected_explicitly() -> None:
    with pytest.raises(ValidationError):
        MessageRunRequest.model_validate(
            {
                "message": {"content": "旧字段不能静默兼容"},
                "run": {
                    "mode": "single_agent",
                    "agent_id": "default",
                    "pending_kind": "steering",
                },
            }
        )

    with pytest.raises(ValidationError):
        PendingRequestPolicyUpdateRequest.model_validate(
            {
                "delivery_policy": "after_turn",
                "enqueue_sequence": 99,
            }
        )
