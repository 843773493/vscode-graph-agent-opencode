"""内部调试状态校验失败由 HTTP 诊断出口处理，业务拒绝仍保持原状态。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import node_debug
from app.api.deps import get_node_debug_service, verify_local_token
from app.core.exceptions import ForbiddenError
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.node_debug import (
    NodeDebugCapabilitiesDTO,
    NodeDebugLaunchProfileDTO,
    NodeDebugStateDTO,
)
from app.services.infrastructure.node_debug.service import NodeDebugService

SESSION_ID = "ses_00000000400070008000000000000001"
THREAD_ID = "thr_00000000700070008000000000000001"
REQUEST_ID = "req-node-debug-validation"
BASE = "/api/v1/debug/node"
OWNER = {"session_id": SESSION_ID, "thread_id": THREAD_ID}
CONFIGURATION_ID = "dbgcfg_" + "1" * 32
CONFIGURATION = {
    "configuration_id": CONFIGURATION_ID,
    "name": "入口调试",
    "created_at": datetime(2026, 10, 31, tzinfo=UTC).isoformat(),
    "updated_at": datetime(2026, 10, 31, tzinfo=UTC).isoformat(),
}
NATIVE_PATHS = ["/private/debug-secret", r"C:\x\debug-secret", r"\\target\x\debug-secret"]
ENDPOINTS = [
    ("GET", "/configurations/" + CONFIGURATION_ID, None, "get_configuration"),
    ("POST", "/configurations", {**OWNER, "name": "入口调试"}, "create_configuration"),
    ("PUT", "/configurations/" + CONFIGURATION_ID, {**OWNER, "name": "入口调试"}, "update_configuration"),
    ("POST", "/configurations/" + CONFIGURATION_ID + "/activate", OWNER, "activate_configuration"),
    ("DELETE", "/configurations/" + CONFIGURATION_ID, None, "delete_configuration"),
    ("POST", "/configurations/import", {**OWNER, "configuration": CONFIGURATION}, "import_configuration"),
    ("POST", "/configurations/" + CONFIGURATION_ID + "/copy", {
        "source_session_id": SESSION_ID, "source_thread_id": THREAD_ID,
        "target_session_id": SESSION_ID, "target_thread_id": THREAD_ID,
    }, "copy_configuration"),
    ("POST", "/start", {**OWNER, "path": "entry.mjs"}, "start"),
    ("POST", "/action", {**OWNER, "action": "continue"}, "apply_action"),
]


@pytest.fixture
def boundary_client() -> Iterator[tuple[TestClient, Mock]]:
    from app.main import app as workspace_app

    service = Mock(spec=NodeDebugService)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(node_debug.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(error_type, workspace_app.exception_handlers[error_type])
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_node_debug_service] = lambda: service
    with TestClient(application, raise_server_exceptions=False, headers={"X-Request-ID": REQUEST_ID}) as client:
        yield client, service


@pytest.mark.parametrize("field", ["program", "working_directory"])
@pytest.mark.parametrize("native", NATIVE_PATHS)
@pytest.mark.parametrize("mutation", ["assignment", "model_copy"])
def test_capability_publication_rejects_mutated_native_reference(
    boundary_client: tuple[TestClient, Mock], field: str, native: str,
    mutation: str, caplog: pytest.LogCaptureFixture,
) -> None:
    client, service = boundary_client
    profile = NodeDebugLaunchProfileDTO(
        name="node-default", adapter="node_inspector", runtime="node", supported=True,
    )
    capabilities = NodeDebugCapabilitiesDTO(
        enabled=True, default_adapter="node_inspector", launch_profiles=[profile],
    )
    if mutation == "assignment":
        setattr(capabilities.launch_profiles[0], field, native)
    else:
        capabilities.launch_profiles[0] = profile.model_copy(update={field: native})
    service.get_capabilities.return_value = capabilities

    response = client.get(BASE + "/capabilities")

    assert response.status_code == 500, response.text
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert response.json()["request_id"] == REQUEST_ID
    assert "debug-secret" not in response.text
    diagnostics = [record for record in caplog.records if record.exc_info]
    assert len(diagnostics) == 1
    assert "PydanticSerializationError" in caplog.text
    service.get_capabilities.assert_called_once()


@pytest.mark.parametrize(("method", "suffix", "payload", "operation"), [
    *ENDPOINTS,
    ("GET", "", None, "get_state"),
    ("GET", "/configurations", None, "list_configurations"),
    ("GET", "/capabilities", None, "get_capabilities"),
])
@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_internal_validation_preserves_original_diagnostic(
    boundary_client: tuple[TestClient, Mock], method: str, suffix: str,
    payload: dict[str, object] | None, operation: str, native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, service = boundary_client
    with pytest.raises(ValidationError) as captured:
        NodeDebugStateDTO.model_validate({**OWNER, "status": {native: native}})
    failure = captured.value
    cause = RuntimeError("内部来源 " + native)
    failure.__cause__ = cause
    getattr(service, operation).side_effect = failure
    response = client.request(method, BASE + suffix, params=OWNER if payload is None else None, json=payload)
    assert response.status_code == 500, response.text
    assert response.json() == {
        "code": 500,
        "message": "请求处理失败: stage=http_request, error=ValidationError",
        "data": None,
        "request_id": REQUEST_ID,
    }
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert "debug-secret" not in response.text
    diagnostics = [record for record in caplog.records if record.exc_info and record.exc_info[1] is failure]
    assert len(diagnostics) == 1 and "debug-secret" in caplog.text
    assert failure.__cause__ is cause
    getattr(service, operation).assert_called_once()


@pytest.mark.parametrize(("method", "suffix", "payload", "operation"), ENDPOINTS)
@pytest.mark.parametrize(("failure", "configuration_status"), [
    (FileNotFoundError("调试目标不存在"), 404),
    (ForbiddenError(), 403),
    (ValueError("调试参数无效"), 400),
    (RuntimeError("调试状态冲突"), 409),
])
def test_business_rejection_keeps_original_status(
    boundary_client: tuple[TestClient, Mock], method: str, suffix: str,
    payload: dict[str, object] | None, operation: str,
    failure: Exception, configuration_status: int,
) -> None:
    client, service = boundary_client
    getattr(service, operation).side_effect = failure
    response = client.request(method, BASE + suffix, params=OWNER if payload is None else None, json=payload)
    expected = 409 if operation == "apply_action" and isinstance(failure, ValueError) else configuration_status
    assert response.status_code == expected, response.text
    detail = failure.readable_message() if isinstance(failure, ForbiddenError) else str(failure)
    assert response.json() == {"detail": detail, "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID
    getattr(service, operation).assert_called_once()


@pytest.mark.parametrize(("suffix", "payload"), [
    ("/configurations", {**OWNER, "name": ""}),
    ("/action", {**OWNER, "action": "set_breakpoint", "params": {"path": "entry.mjs", "line": 0}}),
])
def test_request_validation_precedes_service(
    boundary_client: tuple[TestClient, Mock], suffix: str, payload: dict[str, object],
) -> None:
    client, service = boundary_client
    response = client.post(BASE + suffix, json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    assert service.mock_calls == []
