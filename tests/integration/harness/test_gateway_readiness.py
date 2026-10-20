"""认证就绪探针的真实本地 HTTP 接线；端点为显式测试替身。"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import Mock

import pytest

from tests.support.gateway_processes import wait_for_gateway_ready


@pytest.fixture
def health_endpoint(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[int, list[tuple[str, str | None]]]]:
    status: int = request.param
    observed: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            token = self.headers.get("X-Local-Token")
            observed.append((self.path, token))
            self.send_response(status if token == "local-dev-token" else 401)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server.server_port, observed
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive(), "测试 HTTP 服务线程未停止"


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
