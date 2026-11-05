"""操作 ID 的原始值只进入诊断，日志次错不能覆盖原 400。"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import session_navigation, workspace
from app.api.deps import (
    get_session_catalog_service,
    get_workspace_service,
    verify_local_token,
)
from app.core import failures
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session_navigation.operations import validate_operation_id
from app.services.business.session_navigation.service import SessionCatalogService
from app.services.infrastructure.workspace_service import WorkspaceService

REQUEST_ID = "req-operation-id-unit"
WORKSPACE_ID = "workspace-operation-id-unit"


@pytest.fixture(params=["files", "catalog"])
def operation_boundary(request: pytest.FixtureRequest) -> Iterator[tuple[TestClient, Mock, str]]:
    from app.main import app as workspace_app

    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.add_exception_handler(HTTPException, workspace_app.exception_handlers[HTTPException])
    if request.param == "files":
        application.include_router(workspace.router, prefix="/api/v1")
        service = Mock(spec=WorkspaceService, workspace_id=WORKSPACE_ID)
        operation = service.file_operations.status
        dependency = get_workspace_service
        path = "/api/v1/workspace/files/operations"
    else:
        application.include_router(session_navigation.router, prefix="/api/v1")
        service = Mock(spec=SessionCatalogService, workspace_id=WORKSPACE_ID)
        operation = service.operation_status
        dependency = get_session_catalog_service
        path = "/api/v1/session-catalog/operations"
    application.dependency_overrides[dependency] = lambda: service
    application.dependency_overrides[verify_local_token] = lambda: "local"
    with TestClient(application, raise_server_exceptions=False, headers={
        "X-Request-ID": REQUEST_ID,
        "X-BoxTeam-Gateway-Id": "gateway_789f146c159aa74461af8dacca7341ca",
        "X-BoxTeam-Navigation-Actor": "user:operation-id-test",
    }) as client:
        yield client, operation, path


@pytest.mark.parametrize("native", ["/operation-id-private/secret", r"C:\operation-id-private\secret",
                                   r"\\operation-id-private\share\secret", "file:///operation-id-private/secret"])
@pytest.mark.parametrize("sink_fails", [False, True])
def test_operation_id_error_keeps_diagnostic_and_original_status(
    operation_boundary: tuple[TestClient, Mock, str], native: str, sink_fails: bool,
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, operation, path = operation_boundary
    with pytest.raises(ValueError) as captured:
        validate_operation_id(native)
    failure = captured.value
    operation.side_effect = failure
    sink = Mock(side_effect=OSError("/operation-id-log-sink/private"))
    if sink_fails:
        monkeypatch.setattr(failures.logger, "error", sink)
    response = client.get(path, params={"operation_id": native, "expected_workspace_id": WORKSPACE_ID})
    detail = "operation_id 形态非法：必须为 op_ 加 32 位小写十六进制字符"
    if sink_fails:
        detail += ", diagnostic_sink_error=OSError"
        sink.assert_called_once()
        assert sink.call_args.kwargs["exc_info"] is failure
        assert "operation-id-log-sink" in "\n".join(failure.__notes__)
    else:
        diagnostics = [record for record in caplog.records if record.exc_info and record.exc_info[1] is failure]
        assert len(diagnostics) == 1
        assert "operation-id-private" in caplog.text
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": detail, "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID
    operation.assert_called_once()
