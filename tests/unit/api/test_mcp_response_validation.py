"""MCP 连接管理的内部校验失败沿服务端出口返回，原业务拒绝保持。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import mcp
from app.api.deps import get_mcp_connection_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.mcp import McpConnectionOperationDTO
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.mcp.operation_store import McpConnectionOperationStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

REQUEST_ID = "req-mcp-response-validation"
BASE = "/api/v1/mcp/connection-operations"
OPERATION_ID = "mcp-response-validation"
NATIVE_PATHS = [
    "/private/mcp-api-sentinel",
    r"C:\private\mcp-api-sentinel",
    r"\\target\private\mcp-api-sentinel",
]
CASES = [
    pytest.param(
        "POST",
        "",
        {"operation_id": OPERATION_ID, "server_id": "server", "action": "connect"},
        "submit",
        id="submit",
    ),
    pytest.param("GET", f"/{OPERATION_ID}", None, "status", id="status"),
    pytest.param("POST", f"/{OPERATION_ID}/cancel", None, "cancel", id="cancel"),
    pytest.param(
        "GET",
        f"/{OPERATION_ID}/authorization",
        None,
        "authorization",
        id="authorization",
    ),
    pytest.param(
        "POST",
        f"/{OPERATION_ID}/callback",
        {"code": "private-code", "state": "private-state"},
        "callback",
        id="callback",
    ),
]


@dataclass
class ConnectionBoundaryService:
    error: Exception = field(default_factory=lambda: ValueError("MCP 连接意图不匹配"))
    calls: list[str] = field(default_factory=list)

    def _fail(self, operation: str) -> NoReturn:
        self.calls.append(operation)
        raise self.error

    async def submit(self, request: object) -> NoReturn:
        self._fail("submit")

    async def status(self, operation_id: str, *, wait_timeout_ms: int = 0) -> NoReturn:
        self._fail("status")

    async def cancel(self, operation_id: str) -> NoReturn:
        self._fail("cancel")

    def authorization(self, operation_id: str) -> NoReturn:
        self._fail("authorization")

    def complete_authorization(
        self, operation_id: str, *, code: str, state: str
    ) -> NoReturn:
        self._fail("callback")


@pytest.fixture
def boundary_client() -> Iterator[tuple[TestClient, ConnectionBoundaryService]]:
    from app.main import app as workspace_app

    service = ConnectionBoundaryService()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(mcp.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(
            error_type, workspace_app.exception_handlers[error_type]
        )
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_mcp_connection_service] = lambda: service
    with TestClient(
        application, raise_server_exceptions=False, headers={"X-Request-ID": REQUEST_ID}
    ) as client:
        yield client, service


@pytest.mark.parametrize(("method", "suffix", "payload", "operation"), CASES)
@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_internal_validation_remains_a_server_failure(
    boundary_client: tuple[TestClient, ConnectionBoundaryService],
    method: str,
    suffix: str,
    payload: dict[str, object] | None,
    operation: str,
    native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, service = boundary_client
    with pytest.raises(ValidationError) as captured:
        McpConnectionOperationDTO.model_validate(
            {
                "operation_id": OPERATION_ID,
                "connection_id": "connection",
                "server_id": "server",
                "action": "connect",
                "status": "pending",
                "stage": native,
                "created_at": 1,
                "deadline": 2,
                "updated_at": 1,
            }
        )
    service.error = captured.value
    response = client.request(method, BASE + suffix, json=payload)
    assert response.status_code == 500, response.text
    assert response.json() == {
        "code": 500,
        "message": "请求处理失败: stage=http_request, error=ValidationError",
        "data": None,
        "request_id": REQUEST_ID,
    }
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert (
        "mcp-api-sentinel" not in response.text and "input_value" not in response.text
    )
    assert service.calls == [operation]
    diagnostics = [
        record
        for record in caplog.records
        if record.exc_info and record.exc_info[1] is service.error
    ]
    assert len(diagnostics) == 1 and "mcp-api-sentinel" in caplog.text


@pytest.mark.parametrize(("method", "suffix", "payload", "operation"), CASES)
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (LookupError("MCP 连接操作不存在"), 404),
        (PermissionError("MCP 连接操作已结束或过期"), 409),
        (ValueError("MCP 连接意图不匹配"), 400),
        (TimeoutError("MCP 连接操作清理等待超过期限"), 503),
    ],
)
def test_business_rejection_keeps_its_status_and_request_identity(
    boundary_client: tuple[TestClient, ConnectionBoundaryService],
    method: str,
    suffix: str,
    payload: dict[str, object] | None,
    operation: str,
    error: Exception,
    status: int,
) -> None:
    client, service = boundary_client
    service.error = error
    response = client.request(method, BASE + suffix, json=payload)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": str(error), "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID and service.calls == [
        operation
    ]


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        (
            "POST",
            "",
            {"operation_id": OPERATION_ID, "server_id": "server", "action": "hold"},
        ),
        ("GET", f"/{OPERATION_ID}?wait_timeout_ms=-1", None),
        ("POST", f"/{OPERATION_ID}/callback", {"code": "", "state": "private-state"}),
    ],
)
def test_request_validation_precedes_service_access(
    boundary_client: tuple[TestClient, ConnectionBoundaryService],
    method: str,
    suffix: str,
    payload: dict[str, object] | None,
) -> None:
    client, service = boundary_client
    response = client.request(method, BASE + suffix, json=payload)
    assert response.status_code == 422, response.text
    assert (
        response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    )
    assert service.calls == []


@pytest.fixture
def stored_operation(
    boundary_client: tuple[TestClient, ConnectionBoundaryService],
    empty_workspace_scope: tuple[str, Path],
) -> Iterator[tuple[TestClient, WorkspaceStateStore, McpConnectionOperationDTO]]:
    client, _ = boundary_client
    workspace_id, workspace_root = empty_workspace_scope
    store = WorkspaceStateStore(workspace_root=workspace_root)
    owner = McpCatalogOwner(
        raw_config={"servers": {}},
        workspace_root=workspace_root,
        event_service=EventChannelService(),
        leases=ExternalResourceLeaseLedger(),
    )
    service = McpConnectionService(owner=owner, store=store, workspace_id=workspace_id)
    original, created = McpConnectionOperationStore(store).begin(
        operation_id=OPERATION_ID,
        connection_id=f"mcp-server:v1:{workspace_id}:server",
        server_id="server",
        action="connect",
        configuration_ref="original-config",
        deadline=9999999999,
    )
    assert created
    assert isinstance(client.app, FastAPI)
    client.app.dependency_overrides[get_mcp_connection_service] = lambda: service
    try:
        yield client, store, original
    finally:
        assert client.portal is not None
        client.portal.call(service.close)
        store.close()


@pytest.mark.parametrize("gateway_id", [None, "gateway_00000000000040008000000000000001"])
def test_operation_receipt_projects_native_workspace_and_request_gateway(
    stored_operation: tuple[TestClient, WorkspaceStateStore, McpConnectionOperationDTO], gateway_id: str | None,
) -> None:
    client, _, original = stored_operation
    headers = {"X-BoxTeam-Gateway-Id": gateway_id} if gateway_id is not None else {}
    response = client.get(BASE + f"/{OPERATION_ID}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == original.model_dump()
    assert body["target"] == {"kind": "workspace", "gateway_id": gateway_id,
                              "workspace_id": original.connection_id.split(":")[2]}
    assert body["protocol_version"] == 1
    assert body["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID


@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_real_store_validation_does_not_rewrite_operation_and_can_be_retried(
    stored_operation: tuple[TestClient, WorkspaceStateStore, McpConnectionOperationDTO],
    native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, original = stored_operation
    with closing(store.connection()) as connection:
        connection.execute(
            "UPDATE mcp_connection_operations SET stage = ? WHERE operation_id = ?",
            (native, OPERATION_ID),
        )
        before = tuple(
            connection.execute("SELECT * FROM mcp_connection_operations").fetchone()
        )
        aliases = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM mcp_connection_operation_aliases"
            )
        ]
    response = client.get(BASE + f"/{OPERATION_ID}")
    assert response.status_code == 500, response.text
    assert (
        response.json()["message"]
        == "请求处理失败: stage=http_request, error=ValidationError"
    )
    assert (
        response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    )
    assert "mcp-api-sentinel" not in response.text and "mcp-api-sentinel" in caplog.text
    diagnostics = [
        record
        for record in caplog.records
        if record.exc_info and isinstance(record.exc_info[1], ValidationError)
    ]
    assert len(diagnostics) == 1
    failure = diagnostics[0].exc_info
    assert failure is not None and isinstance(failure[1], ValidationError)
    assert failure[1].errors()[0]["input"] == native
    with closing(store.connection()) as connection:
        assert (
            tuple(
                connection.execute("SELECT * FROM mcp_connection_operations").fetchone()
            )
            == before
        )
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM mcp_connection_operation_aliases"
            )
        ] == aliases
        connection.execute(
            "UPDATE mcp_connection_operations SET stage = ? WHERE operation_id = ?",
            (original.stage, OPERATION_ID),
        )
    repaired = client.get(BASE + f"/{OPERATION_ID}")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"] == original.model_dump(mode="json")
    assert (
        repaired.json()["request_id"] == repaired.headers["X-Request-ID"] == REQUEST_ID
    )
    assert client.get(BASE).json()["data"] == [original.model_dump(mode="json")]
