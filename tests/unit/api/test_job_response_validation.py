"""内部 Job／审批校验失败沿统一 500 出口公开事实，完整输入只留诊断。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import jobs
from app.api.deps import get_job_service, get_tool_approval_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.job import JobControlResponseDTO

JOB_ID = "job-response-validation"
REQUEST_ID = "req-job-response-validation"
APPROVAL = {"command_id": "command", "checkpoint_id": "checkpoint", "responses": [
    {"approval_id": "approval", "decisions": [{"tool_call_id": "call", "type": "approve"}]},
]}
CASES = [
    pytest.param("GET", "", None, "get", ["get"], 404, id="get"),
    pytest.param("GET", "/steps", None, "list_steps", ["list_steps"], 404, id="steps"),
    pytest.param("GET", "/approvals", None, "get", ["get"], 404, id="approvals-lookup"),
    pytest.param("POST", "/approvals", APPROVAL, "get", ["get"], 404, id="approval-submit-lookup"),
    pytest.param("POST", "/approvals", APPROVAL, "submit", ["get", "submit"], 409, id="approval-submit"),
    pytest.param("POST", "/control", {"action": "pause"}, "control", ["control"], 409, id="control"),
    pytest.param("POST", "/control", {"action": "skip"}, "control", ["control"], 501, id="unsupported-control"),
]


@dataclass
class JobBoundaryService:
    error: Exception = field(default_factory=lambda: ValueError(f"Job {JOB_ID} not found"))
    failure_operation: str = "get"
    calls: list[str] = field(default_factory=list)

    def _call(self, operation: str) -> None:
        self.calls.append(operation)
        if operation == self.failure_operation:
            raise self.error
        if operation != "get":
            raise AssertionError(f"不应执行后续操作: {operation}")

    async def get(self, job_id: str) -> None:
        self._call("get")

    async def list_steps(self, job_id: str) -> None:
        self._call("list_steps")

    async def control(self, job_id: str, payload: object) -> None:
        self._call("control")

    def submit(self, job_id: str, payload: object) -> None:
        self._call("submit")


@pytest.fixture
def job_boundary_client() -> Iterator[tuple[TestClient, JobBoundaryService]]:
    from app.main import app as workspace_app

    service = JobBoundaryService()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(jobs.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(error_type, workspace_app.exception_handlers[error_type])
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_job_service] = lambda: service
    application.dependency_overrides[get_tool_approval_service] = lambda: service
    with TestClient(application, raise_server_exceptions=False, headers={"X-Request-ID": REQUEST_ID}) as client:
        yield client, service


@pytest.mark.parametrize(("method", "suffix", "payload", "operation", "calls", "business_status"), CASES)
@pytest.mark.parametrize("native", ["/private/job-response-sentinel", r"C:\private\job-response-sentinel",
                                    r"\\target\private\job-response-sentinel"])
def test_internal_validation_is_server_failure_without_echo(
    job_boundary_client: tuple[TestClient, JobBoundaryService], method: str, suffix: str,
    payload: dict[str, object] | None, operation: str, calls: list[str], business_status: int,
    native: str, caplog: pytest.LogCaptureFixture,
) -> None:
    client, service = job_boundary_client
    with pytest.raises(ValidationError) as captured:
        JobControlResponseDTO.model_validate({"job_id": JOB_ID, "session_id": "session", "thread_id": "thread",
                                              "status": native, "control_state": {native: native}})
    service.error, service.failure_operation = captured.value, operation
    response = client.request(method, f"/api/v1/jobs/{JOB_ID}{suffix}", json=payload)
    assert response.status_code == 500, response.text
    assert response.json() == {"code": 500, "message": "请求处理失败: stage=http_request, error=ValidationError",
                               "data": None, "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert "job-response-sentinel" not in response.text and "control_state" not in response.text
    assert service.calls == calls
    diagnostics = [record for record in caplog.records if record.exc_info and record.exc_info[1] is service.error]
    assert len(diagnostics) == 1 and "job-response-sentinel" in caplog.text


@pytest.mark.parametrize(("method", "suffix", "payload", "operation", "calls", "business_status"), CASES)
def test_business_rejection_keeps_status_and_request_identity(
    job_boundary_client: tuple[TestClient, JobBoundaryService], method: str, suffix: str,
    payload: dict[str, object] | None, operation: str, calls: list[str], business_status: int,
) -> None:
    client, service = job_boundary_client
    service.failure_operation = operation
    if business_status != 404:
        service.error = ValueError("任务动作被拒绝")
    response = client.request(method, f"/api/v1/jobs/{JOB_ID}{suffix}", json=payload)
    assert response.status_code == business_status, response.text
    assert response.json() == {"detail": str(service.error), "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID and service.calls == calls


@pytest.mark.parametrize(("suffix", "payload"), [
    ("/control", {"action": "pause", "scope": "agent"}),
    ("/approvals", {**APPROVAL, "responses": []}),
])
def test_request_validation_precedes_job_or_approval_access(
    job_boundary_client: tuple[TestClient, JobBoundaryService], suffix: str, payload: dict[str, object],
) -> None:
    client, service = job_boundary_client
    response = client.post(f"/api/v1/jobs/{JOB_ID}{suffix}", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    assert service.calls == []
