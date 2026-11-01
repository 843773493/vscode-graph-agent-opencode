"""远端文件 HTTP 失败保持状态和请求身份，普通响应不复制目标诊断正文。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import resources
from app.api.deps import get_resource_navigation, verify_local_token
from app.core import failures
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.resource_platform.sources import (
    gateway_resource_navigation,
)
from app.services.infrastructure.resource_platform.sources.gateway_resource_navigation import (
    GatewayResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

SOURCE = "gateway_" + "a" * 32
TARGET = "gateway_" + "b" * 32
WORKSPACE = "00000000-0000-4000-8000-000000000001"
URI = f"boxteam://{TARGET}/workspace/{WORKSPACE}/resources/files/root/file.txt"
REQUEST_ID = "req-navigation-error"


@dataclass
class NavigationFailure:
    status: int = 500
    detail: object = None
    direct: bool = False
    requests: list[httpx.Request] = field(default_factory=list)
    responses: list[httpx.Response] = field(default_factory=list)


@pytest.fixture
def navigation_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, NavigationFailure]]:
    from app.main import app as workspace_app

    failure = NavigationFailure()

    async def send(outgoing: httpx.Request, *, deadline: float) -> httpx.Response:
        failure.requests.append(outgoing)
        response = httpx.Response(failure.status, request=outgoing,
                                  json={"detail": failure.detail, "request_id": REQUEST_ID},
                                  headers={"X-Request-ID": REQUEST_ID})
        failure.responses.append(response)
        if failure.direct:
            response.raise_for_status()
        return response

    monkeypatch.setattr(gateway_resource_navigation, "send_navigation_request", send)
    transport = GatewayResourceNavigation(
        workspace_id=WORKSPACE, connection=GatewayTransportConnection(gateway_url="http://gateway.test"),
        local_token_supplier=lambda: "navigation-local-test-token",
    )
    owner = ResourceNavigation(workspace_root=tmp_path, workspace_id=WORKSPACE,
                               file_paths=WorkspaceFilePaths(workspace=tmp_path, workspace_id=WORKSPACE, bindings=None),
                               gateway_navigation=transport)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(resources.router, prefix="/api/v1")
    application.add_exception_handler(HTTPException, workspace_app.exception_handlers[HTTPException])
    application.dependency_overrides[verify_local_token] = lambda: "navigation-local-test-token"
    application.dependency_overrides[get_resource_navigation] = lambda: owner
    with TestClient(application, raise_server_exceptions=False,
                    headers={"X-Request-ID": REQUEST_ID, "X-BoxTeam-Gateway-Id": SOURCE}) as client:
        yield client, failure


@pytest.mark.parametrize("status", [302, 404, 500])
@pytest.mark.parametrize("native", ["/private/navigation-http-sentinel", r"C:\private\navigation-http-sentinel",
                                    r"\\target\private\navigation-http-sentinel"])
@pytest.mark.parametrize("direct", [False, True], ids=["decoded", "transport-raised"])
def test_remote_http_failure_does_not_republish_response_detail(
    navigation_api: tuple[TestClient, NavigationFailure], caplog: pytest.LogCaptureFixture,
    status: int, native: str, direct: bool,
) -> None:
    client, failure = navigation_api
    detail = {"message": f"读取目标失败: {native}", "trace": {native: [native]}}
    failure.status, failure.detail, failure.direct = status, detail, direct
    response = client.post("/api/v1/resources/navigation", json={"uri": URI, "operation": "read"})
    assert response.status_code == status, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == REQUEST_ID
    assert "navigation-http-sentinel" not in response.text
    assert URI in response.json()["detail"] and f"status={status}" in response.json()["detail"]
    assert len(failure.requests) == len(failure.responses) == 1
    assert failure.responses[0].json()["detail"] == detail
    assert failure.requests[0].headers["X-Request-ID"] == REQUEST_ID
    diagnostics = [record for record in caplog.records if record.exc_info and isinstance(record.exc_info[1], httpx.HTTPStatusError)]
    assert len(diagnostics) == 1 and "navigation-http-sentinel" in caplog.text


def test_remote_http_failure_survives_diagnostic_sink_error(
    navigation_api: tuple[TestClient, NavigationFailure], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, failure = navigation_api
    failure.detail = "/private/navigation-http-sentinel"
    logged: list[BaseException] = []
    sink_error = OSError(5, "诊断写入失败", "/private/navigation-log-sentinel")

    def broken_sink(
        _message: str, *args: object, exc_info: BaseException,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        assert extra is None
        logged.append(exc_info)
        raise sink_error

    monkeypatch.setattr(failures.logger, "error", broken_sink)
    response = client.post("/api/v1/resources/navigation", json={"uri": URI, "operation": "read"})
    assert response.status_code == 500 and response.json()["request_id"] == REQUEST_ID
    assert "navigation-http-sentinel" not in response.text and "navigation-log-sentinel" not in response.text
    assert "diagnostic_sink_error=OSError" in response.json()["detail"]
    original, = logged
    assert isinstance(original, httpx.HTTPStatusError) and original.response is failure.responses[0]
    notes = "\n".join(original.__notes__)
    assert "navigation-http-sentinel" in notes and "navigation-log-sentinel" in notes
