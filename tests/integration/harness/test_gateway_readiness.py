"""认证就绪探针的真实本地 HTTP 接线；端点为显式测试替身。"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import Mock

import pytest

from tests.support.processes.gateway import wait_for_gateway_ready


@dataclass(slots=True)
class _GatewayHealthResponse:
    status: int = 200
    observed_requests: list[tuple[str, str | None]] = field(default_factory=list)


@pytest.fixture(scope="module")
def health_server() -> Iterator[tuple[int, _GatewayHealthResponse]]:
    response = _GatewayHealthResponse()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            token = self.headers.get("X-Local-Token")
            response.observed_requests.append((self.path, token))
            status = response.status if token == "local-dev-token" else 401
            self.send_response(status)
            self.end_headers()

    with ExitStack() as lifetime:
        server = HTTPServer(("127.0.0.1", 0), Handler)
        lifetime.callback(server.server_close)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        def join_server_thread() -> None:
            thread.join(timeout=5)
            assert not thread.is_alive(), "测试 HTTP 服务线程未停止"

        lifetime.callback(join_server_thread)
        lifetime.callback(server.shutdown)
        yield server.server_port, response


@pytest.fixture
def health_endpoint(
    request: pytest.FixtureRequest,
    health_server: tuple[int, _GatewayHealthResponse],
) -> tuple[int, list[tuple[str, str | None]]]:
    response_status = request.param
    port, response = health_server
    response.status = response_status
    response.observed_requests.clear()
    return port, response.observed_requests


@pytest.fixture
def process() -> Mock:
    value = Mock(spec=subprocess.Popen)
    value.pid = 4312
    value.poll.return_value = None
    return value


@pytest.mark.parametrize("health_endpoint", [200], indirect=True)
def test_gateway_health_authentication_reaches_http_server(
    health_endpoint: tuple[int, list[tuple[str, str | None]]],
    process: Mock,
) -> None:
    port, observed = health_endpoint
    wait_for_gateway_ready(port, process)
    assert observed == [("/api/gateway/health", "local-dev-token")]


@pytest.mark.parametrize("health_endpoint", [403], indirect=True)
def test_gateway_health_rejection_is_a_single_http_request(
    health_endpoint: tuple[int, list[tuple[str, str | None]]],
    process: Mock,
) -> None:
    port, observed = health_endpoint
    with pytest.raises(RuntimeError, match="status=403"):
        wait_for_gateway_ready(port, process)
    assert observed == [("/api/gateway/health", "local-dev-token")]
