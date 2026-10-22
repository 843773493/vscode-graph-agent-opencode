"""中断入口保留 main 默认范围，显式 Thread 只能使用 canonical 身份。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_session_interrupt_service, verify_local_token
from app.api.sessions import router
from app.core.exceptions import NotFoundError
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session import SessionInterruptResultDTO

SESSION = "ses_019bef80066f7038ae8e88ccb2e5e2ea"
THREAD = "thr_019bef80066f7038ae8e88ccb2e5e2eb"


@pytest.fixture
def interrupt_service():
    return SimpleNamespace(interrupt=AsyncMock(return_value=SessionInterruptResultDTO(
        session_id=SESSION, thread_id=THREAD, job_id="job_interrupt", status="cancelling",
        interrupt_request_id="intr_identity", phase="tool", tool_name="read_file",
    )))


@pytest.fixture
def interrupt_client(interrupt_service):
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_session_interrupt_service] = lambda: interrupt_service
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    with TestClient(application) as client:
        yield client


@pytest.mark.parametrize("thread_id", [None, THREAD])
def test_interrupt_forwards_scope_and_returns_exact_identity(interrupt_client, interrupt_service, thread_id):
    response = interrupt_client.post(f"/api/v1/sessions/{SESSION}/interrupt", params={} if thread_id is None else {"thread_id": thread_id})
    assert response.status_code == 200, response.text
    interrupt_service.interrupt.assert_awaited_once_with(session_id=SESSION, thread_id=thread_id)
    body = response.json()
    assert body["data"]["thread_id"] == THREAD
    assert body["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize("thread_id", ["", "null", SESSION, "thr_legacy"])
def test_invalid_thread_is_rejected_before_interrupt(interrupt_client, interrupt_service, thread_id):
    response = interrupt_client.post(f"/api/v1/sessions/{SESSION}/interrupt", params={"thread_id": thread_id})
    assert response.status_code == 422, response.text
    interrupt_service.interrupt.assert_not_awaited()


@pytest.mark.parametrize("failure,status", [
    (NotFoundError("Thread 不存在"), 404),
    (SessionDeletionPendingError("Session 正在删除"), 409),
])
def test_interrupt_owner_failure_is_explicit(interrupt_client, interrupt_service, failure, status):
    interrupt_service.interrupt.side_effect = failure
    response = interrupt_client.post(f"/api/v1/sessions/{SESSION}/interrupt", params={"thread_id": THREAD})
    assert response.status_code == status, response.text
