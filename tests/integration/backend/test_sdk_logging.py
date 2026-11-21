"""产品 TraceMiddleware 与 SDK 日志控制路由的 HTTP 合同。"""

from __future__ import annotations

import io
import logging

import pytest
from boxteam_plugin_sdk.logging import configure_application_logging

from tests.support.runtime_logging import isolated_root_logger as isolated_root_logger


@pytest.mark.asyncio
async def test_internal_http_control_requires_private_token_and_loopback(
    isolated_root_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from boxteam_plugin_sdk.http import verify_runtime_control_token
    from boxteam_plugin_sdk.logging import runtime_logging_router
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.core.trace_middleware import TraceMiddleware

    configure_application_logging(service="backend", stream=io.StringIO())
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(runtime_logging_router(prefix="/control", verify_token=verify_runtime_control_token))
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", "private-gateway-token")
    transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        for token in (None, "local-dev-token", "wrong-token"):
            headers = {} if token is None else {"X-Local-Token": token}
            assert (await client.get("/control", headers=headers)).status_code == 401
        headers = {"X-Local-Token": "private-gateway-token"}
        for browser_header in ({"Origin": "http://localhost"}, {"Sec-Fetch-Site": "same-origin"}):
            assert (await client.get("/control", headers=headers | browser_header)).status_code == 403
        state_response = await client.get("/control", headers=headers)
        assert state_response.status_code == 200
        assert state_response.headers["X-Request-ID"] == state_response.json()["request_id"]
        state = state_response.json()["data"]
        payload = {"runtime_instance_id": state["runtime_instance_id"], "revision": "http-r1",
                       "expected_revision": None, "level": "WARNING", "pretty": False}
        for invalid in ({"pretty": "false"}, {"pretty": 0}, {"level": "warn"},
                        {"revision": " "}, {"unknown": 1}, {"expected_revision": 1}):
            assert (await client.post("/control", headers=headers, json=payload | invalid)).status_code == 422
        applied = await client.post("/control", headers=headers, json=payload)
        assert applied.status_code == 200
        assert applied.json()["data"]["level"] == "warning"
        assert applied.json()["data"]["applied_revision"] == "http-r1"
        assert applied.headers["X-Request-ID"] == applied.json()["request_id"]
        conflict = await client.post("/control", headers=headers, json=payload | {"runtime_instance_id": "old"})
        assert conflict.status_code == 409
        monkeypatch.delenv("BOXTEAM_RUNTIME_CONTROL_TOKEN")
        assert (await client.get("/control", headers=headers)).status_code == 503
    async with AsyncClient(transport=ASGITransport(app=app, client=("192.0.2.1", 1234)), base_url="http://localhost") as client:
        assert (await client.get("/control")).status_code == 403
