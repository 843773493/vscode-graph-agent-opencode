from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext
from app.gateway.server import workspace_proxy

GATEWAY_ID = "gateway_00000000000040008000000000000001"
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
SESSION_ID = "ses_00000000000070008000000000000001"
THREAD_ID = "thr_00000000000070008000000000000002"


@pytest.fixture
def dap_workspace_proxy(monkeypatch: pytest.MonkeyPatch):
    target = SimpleNamespace(
        gateway_id=GATEWAY_ID,
        workspace_id=WORKSPACE_ID,
        key=(GATEWAY_ID, WORKSPACE_ID),
        connection_kind="local",
    )
    lease = SimpleNamespace(invalidated=asyncio.Event(), gateway_id=GATEWAY_ID,
                            workspace_id=WORKSPACE_ID)
    released: list[tuple[object, bool]] = []
    forwarded: list[str] = []
    forwarded_event = threading.Event()
    connect_options: list[dict[str, object]] = []
    close_upstream = asyncio.Event()

    class Registry:
        gateway_id = GATEWAY_ID

        def resolve_request_target(self, workspace_id: str, gateway_id: str):
            if (workspace_id, gateway_id) != (WORKSPACE_ID, GATEWAY_ID):
                raise LookupError("目标不在当前 Gateway")
            return target

        def acquire_route_reference(self, workspace_id, *, streaming, gateway_connection_id=None):
            assert workspace_id == target.key
            assert streaming is True
            assert gateway_connection_id is None
            return lease

        def resolve_service_url(self, workspace_id, service):
            assert workspace_id == target.key and service == "workspace_api"
            return "http://127.0.0.1:49152"

        def target_request_headers(self, resolved, *, gateway_connection_id=None):
            assert resolved is target and gateway_connection_id is None
            return {"X-Local-Token": "backend-local-token", "X-BoxTeam-Workspace-Id": WORKSPACE_ID}

        def release_route_reference(self, workspace_id, *, streaming):
            released.append((workspace_id, streaming))

    registry = Registry()
    monkeypatch.setattr(workspace_proxy, "_registry", lambda _connection: registry)
    monkeypatch.setattr(
        workspace_proxy,
        "verify_gateway_access",
        lambda **_kwargs: GatewayAuthContext(kind="local"),
    )

    class Upstream:
        subprotocol = None

        async def send(self, message: str | bytes) -> None:
            forwarded.append(message)
            forwarded_event.set()

        async def __aiter__(self):
            yield "backend-dap-frame"
            await close_upstream.wait()

    upstream = Upstream()

    class UpstreamContext:
        async def __aenter__(self):
            return upstream

        async def __aexit__(self, *_args):
            close_upstream.set()

    def fake_connect(url: str, **kwargs):
        connect_options.append({"url": url, **kwargs})
        return UpstreamContext()

    monkeypatch.setattr(workspace_proxy, "connect", fake_connect)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_proxy.router)
    app.state.registry = registry
    app.state.managed_runtime_restore_tasks = {}
    return app, connect_options, forwarded, forwarded_event, released, target


def _dap_path(*, gateway_id: str = GATEWAY_ID) -> str:
    return (
        f"/api/v1/debug/node/runs/run_frozen/dap?token=browser-local-token"
        f"&gateway_id={gateway_id}&workspace_id={WORKSPACE_ID}"
        f"&session_id={SESSION_ID}&thread_id={THREAD_ID}"
    )


def test_debug_dap_route_uses_exact_workspace_backend_and_strips_browser_token(
    dap_workspace_proxy,
) -> None:
    app, connect_options, forwarded, forwarded_event, released, _target = dap_workspace_proxy
    with TestClient(app) as client, client.websocket_connect(_dap_path()) as socket:
        assert socket.receive_text() == "backend-dap-frame"
        socket.send_text("dap-client-frame")
        assert forwarded_event.wait(2)

    assert forwarded == ["dap-client-frame"]
    assert len(connect_options) == 1
    options = connect_options[0]
    parsed = urlsplit(options["url"])
    assert parsed.scheme == "ws"
    assert parsed.netloc == "127.0.0.1:49152"
    assert parsed.path == "/api/v1/debug/node/runs/run_frozen/dap"
    assert parse_qs(parsed.query) == {
        "gateway_id": [GATEWAY_ID],
        "workspace_id": [WORKSPACE_ID],
        "session_id": [SESSION_ID],
        "thread_id": [THREAD_ID],
    }
    headers = {key.lower(): value for key, value in options["additional_headers"].items()}
    assert headers["x-local-token"] == "backend-local-token"
    assert headers["x-boxteam-workspace-id"] == WORKSPACE_ID
    assert headers["x-boxteam-gateway-id"] == GATEWAY_ID
    assert not any(key.startswith("sec-websocket-") for key in headers)
    assert "browser-local-token" not in str(headers)
    assert released == [((GATEWAY_ID, WORKSPACE_ID), True)]


def test_debug_dap_route_rejects_gateway_workspace_mismatch_before_connecting(
    dap_workspace_proxy,
) -> None:
    app, connect_options, _forwarded, _event, released, _target = dap_workspace_proxy
    other_gateway = "gateway_00000000000040008000000000000002"
    with pytest.raises(WebSocketDenialResponse) as denied, TestClient(app) as client, client.websocket_connect(
        _dap_path(gateway_id=other_gateway),
    ):
        pass
    assert denied.value.status_code == 409
    assert connect_options == []
    assert released == []
