from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.messages import (
    create_message_and_run,
    list_main_thread_pending_requests,
    list_pending_requests,
    update_pending_request_policy,
)
from app.core.session_lifecycle_gate import SessionDeletionPendingError
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


@pytest.fixture
def scoped_pending_readers(request: pytest.FixtureRequest):
    calls = []
    main_thread = "thr_018f0000000070008000000000000001"
    error = getattr(request, "param", None)

    class SessionReader:
        @asynccontextmanager
        async def session_read_admission(self, session_id):
            calls.append(("enter", session_id))
            if error is not None:
                raise error
            yield
            calls.append(("exit", session_id))

        async def resolve_main_thread(self, session_id):
            calls.append(("resolve", session_id))
            return main_thread

    class PendingReader:
        async def list_pending(self, session_id, *, thread_id=None):
            calls.append(("pending", session_id, thread_id))
            if error is not None:
                raise error
            return PendingRequestListDTO(session_id=session_id, thread_id=thread_id)

    return PendingReader(), SessionReader(), calls, main_thread


@pytest.mark.asyncio
async def test_main_pending_route_resolves_canonical_scope_while_session_route_aggregates(
    scoped_pending_readers,
) -> None:
    job_service, session_service, calls, main_thread = scoped_pending_readers
    session_id = "ses_018f0000000070008000000000000001"
    main = await list_main_thread_pending_requests(
        session_id, _="local", request_id="request_scope",
        job_service=job_service, session_service=session_service,
    )
    aggregate = await list_pending_requests(
        session_id, _="local", request_id="request_scope", job_service=job_service,
    )
    assert main.data.thread_id == main_thread and aggregate.data.thread_id is None
    assert calls == [
        ("enter", session_id), ("resolve", session_id),
        ("pending", session_id, main_thread), ("exit", session_id),
        ("pending", session_id, None),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("main_scope", [False, True])
@pytest.mark.parametrize("scoped_pending_readers", [
    SessionDeletionPendingError("session_deletion_pending: 原 catalog 正在删除"),
    RuntimeError("原 pending owner 完整性故障"),
], indirect=True)
async def test_pending_read_maps_deleting_and_preserves_real_owner_failure(
    scoped_pending_readers, main_scope: bool,
) -> None:
    job_service, session_service, calls, _main_thread = scoped_pending_readers
    session_id = "ses_018f0000000070008000000000000001"
    try:
        if main_scope:
            await list_main_thread_pending_requests(
                session_id, _="local", request_id="request_deleting",
                job_service=job_service, session_service=session_service,
            )
        else:
            await list_pending_requests(
                session_id, _="local", request_id="request_deleting", job_service=job_service,
            )
    except HTTPException as error:
        assert error.status_code == 409
        assert error.detail == "session_deletion_pending: 原 catalog 正在删除"
    except RuntimeError as error:
        assert type(error) is RuntimeError
        assert str(error) == "原 pending owner 完整性故障"
    else:
        pytest.fail("失败的 pending owner 不能返回虚假的队列")
    assert calls == ([("enter", session_id)] if main_scope else [("pending", session_id, None)])


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
