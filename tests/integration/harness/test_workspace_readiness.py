"""工作区就绪探针的真实 HTTP 接线；端点是显式测试替身。"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import Mock

import pytest

from tests.support.processes.workspace import wait_for_backend_ready

EXPECTED_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


@dataclass(slots=True)
class _HealthResponse:
    status: int = 200
    payload: dict[str, object] = field(default_factory=dict)
    observed_requests: list[tuple[str, str | None]] = field(default_factory=list)


@pytest.fixture(scope="module")
def health_server() -> Iterator[tuple[int, _HealthResponse]]:
    response = _HealthResponse()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            response.observed_requests.append(
                (self.path, self.headers.get("X-BoxTeam-Workspace-Id"))
            )
            body = json.dumps(response.payload).encode()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
    health_server: tuple[int, _HealthResponse],
) -> tuple[int, list[tuple[str, str | None]]]:
    response_status, response_payload = request.param
    port, response = health_server
    response.status = response_status
    response.payload = response_payload
    response.observed_requests.clear()
    return port, response.observed_requests


@pytest.fixture
def process() -> Mock:
    value = Mock(spec=subprocess.Popen)
    value.pid = 4312
    value.poll.return_value = None
    return value


@pytest.mark.parametrize(
    "health_endpoint, expected_error",
    [
        pytest.param((200, {"workspace_id": EXPECTED_WORKSPACE_ID}), None, id="ready"),
        pytest.param((200, {"workspace_id": "another-workspace"}), "UUID 不匹配", id="wrong-owner"),
        pytest.param((200, {}), "UUID 不匹配", id="missing-owner"),
        pytest.param((202, {"workspace_id": EXPECTED_WORKSPACE_ID}), "status=202", id="not-ready-status"),
        pytest.param((403, {"error": "wrong owner"}), "status=403.*wrong owner", id="rejected"),
    ],
    indirect=["health_endpoint"],
)
def test_workspace_readiness_validates_identity_through_http(
    health_endpoint: tuple[int, list[tuple[str, str | None]]],
    expected_error: str | None,
    process: Mock,
) -> None:
    port, observed = health_endpoint
    if expected_error is None:
        wait_for_backend_ready(port, process, EXPECTED_WORKSPACE_ID)
    else:
        with pytest.raises(RuntimeError, match=expected_error):
            wait_for_backend_ready(port, process, EXPECTED_WORKSPACE_ID)
    assert observed == [("/api/v1/health", EXPECTED_WORKSPACE_ID)]
