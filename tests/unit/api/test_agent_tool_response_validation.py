"""Agent／工具管理的内部校验与业务拒绝必须沿各自的 HTTP 出口。"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import agents, tools
from app.api.deps import (
    get_agent_service,
    get_tool_service,
    get_tool_test_service,
    verify_local_token,
)
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.agent import AgentDTO
from app.services.business.agent_service import AgentService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.tool_service import ToolNotFoundError

REQUEST_ID = "req-agent-tool-validation"
CASES = [
    pytest.param("GET", "/agents/default", None, "get", 404, id="agent-get"),
    pytest.param(
        "PUT",
        "/agents/workspace-default",
        {"agent_id": "default"},
        "set_agent",
        404,
        id="agent-default",
    ),
    pytest.param(
        "PUT",
        "/agents/default/workspace-default-provider",
        {"provider_id": "primary"},
        "set_provider",
        404,
        id="provider-default",
    ),
    pytest.param("GET", "/tools", None, "list", 400, id="tool-list"),
    pytest.param("GET", "/tools/read_file", None, "get", 400, id="tool-get"),
    pytest.param(
        "POST", "/tools/read_file/tests", {}, "start", 400, id="tool-test-start"
    ),
]


@dataclass
class BoundaryService:
    error: Exception = field(
        default_factory=lambda: ValueError("指定的 Agent 或工具不可用")
    )
    calls: list[str] = field(default_factory=list)

    def _fail(self, operation: str) -> NoReturn:
        self.calls.append(operation)
        raise self.error

    async def get(self, identity: str, agent_id: str = "default") -> NoReturn:
        self._fail("get")

    async def list(self, agent_id: str = "default") -> NoReturn:
        self._fail("list")

    async def set_workspace_default_agent(self, agent_id: str) -> NoReturn:
        self._fail("set_agent")

    async def set_workspace_default_provider(
        self, agent_id: str, provider_id: str
    ) -> NoReturn:
        self._fail("set_provider")

    async def start(self, *, tool_name: str, request: object) -> NoReturn:
        self._fail("start")


@pytest.fixture
def boundary_client() -> Iterator[tuple[TestClient, BoundaryService]]:
    from app.main import app as workspace_app

    service = BoundaryService()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(agents.router, prefix="/api/v1")
    application.include_router(tools.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(
            error_type, workspace_app.exception_handlers[error_type]
        )
    application.dependency_overrides[verify_local_token] = lambda: "local"
    for dependency in (get_agent_service, get_tool_service, get_tool_test_service):
        application.dependency_overrides[dependency] = lambda: service
    with TestClient(
        application, raise_server_exceptions=False, headers={"X-Request-ID": REQUEST_ID}
    ) as client:
        yield client, service


@pytest.mark.parametrize(
    ("method", "path", "payload", "operation", "business_status"), CASES
)
@pytest.mark.parametrize(
    "native",
    [
        "/private/agent-tool-sentinel",
        r"C:\private\agent-tool-sentinel",
        r"\\target\private\agent-tool-sentinel",
    ],
)
def test_internal_validation_is_server_failure_without_echo(
    boundary_client: tuple[TestClient, BoundaryService],
    method: str,
    path: str,
    payload: dict[str, object] | None,
    operation: str,
    business_status: int,
    native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, service = boundary_client
    with pytest.raises(ValidationError) as captured:
        AgentDTO.model_validate(
            {
                "agent_id": "default",
                "name": {native: native},
                "model": "model",
                "tools": [],
                "capabilities": [],
                "providers": [],
            }
        )
    service.error = captured.value
    response = client.request(method, "/api/v1" + path, json=payload)
    assert response.status_code == 500, response.text
    assert response.json() == {
        "code": 500,
        "message": "请求处理失败: stage=http_request, error=ValidationError",
        "data": None,
        "request_id": REQUEST_ID,
    }
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert (
        "agent-tool-sentinel" not in response.text
        and "input_value" not in response.text
    )
    assert service.calls == [operation]
    diagnostics = [
        record
        for record in caplog.records
        if record.exc_info and record.exc_info[1] is service.error
    ]
    assert len(diagnostics) == 1 and "agent-tool-sentinel" in caplog.text


@pytest.mark.parametrize(
    ("method", "path", "payload", "operation", "business_status"), CASES
)
def test_business_rejection_keeps_status_and_request_identity(
    boundary_client: tuple[TestClient, BoundaryService],
    method: str,
    path: str,
    payload: dict[str, object] | None,
    operation: str,
    business_status: int,
) -> None:
    client, service = boundary_client
    response = client.request(method, "/api/v1" + path, json=payload)
    assert response.status_code == business_status, response.text
    assert response.json() == {"detail": str(service.error), "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID and service.calls == [
        operation
    ]


@pytest.mark.parametrize(
    ("method", "path", "payload", "error", "status"),
    [
        (
            "PUT",
            "/agents/default/workspace-default-provider",
            {"provider_id": "primary"},
            TypeError("默认配置结构损坏"),
            409,
        ),
        ("POST", "/tools/read_file/tests", {}, RuntimeError("工具测试已在运行"), 409),
        ("GET", "/tools/missing", None, ToolNotFoundError("工具不存在"), 404),
    ],
)
def test_specific_business_error_keeps_existing_classification(
    boundary_client: tuple[TestClient, BoundaryService],
    method: str,
    path: str,
    payload: dict[str, object] | None,
    error: Exception,
    status: int,
) -> None:
    client, service = boundary_client
    service.error = error
    response = client.request(method, "/api/v1" + path, json=payload)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": str(error), "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID and len(service.calls) == 1


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/agents/workspace-default", {}),
        ("/agents/default/workspace-default-provider", {}),
        ("/tools/read_file/tests", {"repetitions": 0}),
    ],
)
def test_bad_request_is_rejected_before_service_access(
    boundary_client: tuple[TestClient, BoundaryService],
    path: str,
    payload: dict[str, object],
) -> None:
    client, service = boundary_client
    response = client.request(
        "POST" if path.startswith("/tools") else "PUT", "/api/v1" + path, json=payload
    )
    assert response.status_code == 422, response.text
    assert (
        response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    )
    assert service.calls == []


@pytest.fixture
def config_service(tmp_path: Path) -> ConfigService:
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_bytes(Path("configs/workspace_inline.jsonc").read_bytes())
    return ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)


@pytest.mark.parametrize(
    "native",
    [
        "/private/agent-catalog-sentinel",
        r"C:\private\agent-catalog-sentinel",
        r"\\target\private\agent-catalog-sentinel",
    ],
)
def test_real_agent_catalog_validation_can_be_retried_after_repair(
    boundary_client: tuple[TestClient, BoundaryService],
    config_service: ConfigService,
    monkeypatch: pytest.MonkeyPatch,
    native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, service = boundary_client
    catalog = deepcopy(config_service.list_agents())
    original_name = catalog["default"]["name"]
    catalog["default"]["name"] = {native: native}
    monkeypatch.setattr(config_service, "list_agents", lambda: catalog)
    monkeypatch.setattr(
        "app.services.business.agent_service.provider_configuration_error",
        lambda *_args: None,
    )
    monkeypatch.setattr(service, "get", AgentService(config_service=config_service).get)
    response = client.get("/api/v1/agents/default")
    assert response.status_code == 500, response.text
    assert (
        response.json()["message"]
        == "请求处理失败: stage=http_request, error=ValidationError"
    )
    assert (
        response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    )
    assert (
        "agent-catalog-sentinel" not in response.text
        and "agent-catalog-sentinel" in caplog.text
    )
    catalog["default"]["name"] = original_name
    repaired = client.get("/api/v1/agents/default")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"]["agent_id"] == "default"
    assert repaired.json()["data"]["name"] == original_name
    assert (
        repaired.json()["request_id"] == repaired.headers["X-Request-ID"] == REQUEST_ID
    )
