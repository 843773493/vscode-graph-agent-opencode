"""上游异常保留原响应与租约清理，同时提供 request_id 关联的完整日志。"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.server.workspace_proxy import _proxy_workspace_request
from tests.unit.gateway.server.test_workspace_proxy_route_reference import (
    _hermetic_gateway_env as _hermetic_gateway_env,  # noqa: PLC0414
)
from tests.unit.gateway.server.test_workspace_proxy_route_reference import _make_request


@pytest.mark.parametrize("error_type,status,stage", [
    (httpx.ConnectError, 502, "upstream_request"),
    (httpx.ReadError, 502, "upstream_request"),
    (httpx.RemoteProtocolError, 502, "upstream_request"),
    (httpx.PoolTimeout, 502, "upstream_request"),
    (TimeoutError, 504, "upstream_headers"),
])
async def test_upstream_failure_logs_cause_without_retry_or_route_leak(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    error_type: type[Exception], status: int, stage: str,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(WorkspaceTarget(
        workspace_id="diagnostic-target", name="上游错误诊断",
        root_path=str(tmp_path / "workspace"), backend_url="http://127.0.0.1:41001",
        connection_kind="local", backend_workspace_id="00000000-0000-4000-8000-000000000001",
    ), activate=False)
    cause = OSError(111, "测试底层连接诊断")
    failure = error_type("测试上游请求故障")
    calls = 0

    async def fail_upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["X-Request-ID"] == "req_route_ref"
        raise failure from cause

    caplog.set_level(logging.ERROR)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_upstream)) as client:
        app = FastAPI()
        app.state.registry = registry
        app.state.http_client = client
        app.state.streaming_http_client = client
        request = _make_request(app, method="GET", path="/api/v1/jobs/job_1", workspace_id="diagnostic-target")
        with pytest.raises(HTTPException) as caught:
            await _proxy_workspace_request("jobs/job_1", request, auth=None, user_access=None, include_credentials=False)

    assert caught.value.status_code == status
    assert caught.value.__cause__ is failure and failure.__cause__ is cause
    assert calls == 1, "普通请求不因诊断日志而自动重试"
    assert registry.route_reference_counts("diagnostic-target") == (0, 0)
    registry._assert_route_references_drained("diagnostic-target")
    diagnostics = [record for record in caplog.records if record.name == "app.gateway.server.workspace_proxy"]
    assert len(diagnostics) == 1
    assert diagnostics[0].exc_info is not None and diagnostics[0].exc_info[1] is failure
    for expected in ("req_route_ref", "diagnostic-target", "GET", f"stage={stage}", str(cause), str(failure)):
        assert expected in caplog.text
