"""ASGI 消息准入、真实存储与冷读取；执行器只用于固定 FIFO 等待边界。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI

from app.api import messages
from app.api.deps import (
    get_message_service,
    get_session_orchestrator,
    verify_local_token,
)
from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.path_utils import workspace_context
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.message_service import MessageService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)


class HeldExecutor:
    async def run(self, job) -> str:
        job.progress_reporter("agent_loop_ready")
        await asyncio.Future()
        raise AssertionError("执行屏障只能由测试取消")


@dataclass
class SubmissionCase:
    container: AppContainer
    client: httpx.AsyncClient
    session_id: str
    thread_id: str

    @property
    def root(self) -> str:
        return f"/api/v1/sessions/{self.session_id}/threads/{self.thread_id}"

    async def submit(self, payload: dict[str, object]) -> httpx.Response:
        request_id = create_prefixed_id("req")
        response = await self.client.post(self.root + "/messages", json=payload, headers={"X-Request-ID": request_id})
        assert response.headers["X-Request-ID"] == request_id
        if response.is_success:
            assert response.json()["request_id"] == request_id
        return response


@pytest.fixture
async def submission_case(startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch):
    container = startup_container
    monkeypatch.setattr(container.job_service, "_job_executor", HeldExecutor())
    session = await container.session_service.create(SessionCreateRequest(title="消息操作身份"))
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(messages.router, prefix="/api/v1")
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    application.dependency_overrides[get_message_service] = lambda: container.message_service
    application.dependency_overrides[get_session_orchestrator] = lambda: container.session_orchestrator
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        with workspace_context(container.workspace_id, container.workspace_root):
            try:
                yield SubmissionCase(container, client, session.session_id, session.thread_id)
            finally:
                container.job_service.close_admission()
                await container.job_service.force_interrupt_active(reason="message_submission_teardown")
                await container.job_service.drain_finalizations()


def request_body(content: str = "冻结请求") -> dict[str, object]:
    return {"operation_id": create_prefixed_id("op"), "message": {"content": content}, "run": {}}


async def test_response_loss_and_concurrent_retries_keep_one_acceptance(submission_case: SubmissionCase):
    case = submission_case
    payload = request_body()
    replies = await asyncio.gather(*(case.submit(payload) for _ in range(3)))
    assert [reply.status_code for reply in replies] == [200, 200, 200]
    accepted = [reply.json()["data"] for reply in replies]
    assert len({(reply["message_id"], reply["job_id"], reply["turn_id"]) for reply in accepted}) == 1
    assert {reply["operation_id"] for reply in accepted} == {payload["operation_id"]}
    queried = await case.client.get(case.root + f"/message-submissions/{payload['operation_id']}")
    result = queried.json()["data"]
    assert result["status"] == "accepted"
    assert result["accepted"]["job_id"] == accepted[0]["job_id"]
    records = case.container.checkpointer.execution_queue_snapshot(case.session_id, case.thread_id).records
    assert len(records) == 1
    assert records[0].input.submission_hash
    assert records[0].input.execution_binding.admission_idempotency_key == payload["operation_id"]


async def test_identity_payload_conflict_does_not_enqueue_and_equal_text_is_independent(submission_case: SubmissionCase):
    case = submission_case
    payload = request_body()
    first = await case.submit(payload)
    assert first.status_code == 200, first.text
    changed = {**payload, "message": {"content": "不同输入"}}
    conflict = await case.submit(changed)
    assert conflict.status_code == 409, conflict.text
    second = await case.submit(request_body())
    assert second.status_code == 200, second.text
    assert first.json()["data"]["job_id"] != second.json()["data"]["job_id"]
    records = case.container.checkpointer.execution_queue_snapshot(case.session_id, case.thread_id).records
    assert len(records) == 2
    assert [record.cursor.commit_id for record in records] == sorted(record.cursor.commit_id for record in records)


async def test_terminal_submission_cold_read_keeps_identity_without_execution_replay(submission_case: SubmissionCase):
    case = submission_case
    payload = request_body()
    response = await case.submit(payload)
    assert response.status_code == 200, response.text
    original = response.json()["data"]
    await case.container.job_service.force_interrupt_active(reason="cold_read")
    await case.container.job_service.drain_finalizations()
    saver = RolloutCheckpointSaver(sessions_dir=case.container.session_service.path_resolver.sessions_root)
    reader = MessageService(main_thread_resolver=case.container.session_service.path_resolver.main_thread_id,
                            checkpointer=saver, workspace_root=case.container.workspace_root)
    record = reader.message_submission(case.session_id, case.thread_id, payload["operation_id"])
    assert record is not None and record.phase == "terminal"
    assert record.input.message_id == original["message_id"]
    assert record.cursor.admission.job_id == original["job_id"]
    retried = await case.submit(payload)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["status"] == "cancelled"
    assert retried.json()["data"]["job_id"] == original["job_id"]
    assert case.container.checkpointer.execution_queue_snapshot(case.session_id, case.thread_id).records == ()


async def test_unknown_operation_is_not_reported_as_rejected(submission_case: SubmissionCase):
    case = submission_case
    response = await case.client.get(case.root + f"/message-submissions/{create_prefixed_id('op')}")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "unknown"
    assert response.json()["data"]["accepted"] is None
