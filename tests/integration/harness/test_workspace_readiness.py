"""工作区就绪探针的真实 HTTP 接线；端点是显式测试替身。"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

import tests.support.processes.workspace as workspace_processes
from tests.harness.python.run_context import TestRunContext
from tests.support.http_server import running_http_server
from tests.support.ports import TestFilePortBlock, integration_port_block_for_file
from tests.support.processes.workspace import wait_for_backend_ready

EXPECTED_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def parallel_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestFilePortBlock, RuntimeError, list[subprocess.Popen[str]], list[int]]:
    ports = integration_port_block_for_file(Path(__file__))
    failure = RuntimeError("并行节点就绪失败")
    processes: list[subprocess.Popen[str]] = []
    ready_ports: list[int] = []
    original = workspace_processes.wait_for_backend_ready

    def wait(port: int, process: subprocess.Popen[str], workspace_id: str) -> None:
        processes.append(process)
        if port == ports.port(20):
            raise failure
        original(port, process, workspace_id)
        ready_ports.append(port)

    monkeypatch.setattr(workspace_processes, "wait_for_backend_ready", wait)
    return ports, failure, processes, ready_ports


async def test_parallel_federation_waits_for_other_starters_before_failure_cleanup(
    parallel_backend_failure: tuple[TestFilePortBlock, RuntimeError, list[subprocess.Popen[str]], list[int]],
) -> None:
    from tests.integration.backend.sessions.test_session_communication_pipeline import (
        _prepare_workspace,
    )
    from tests.support.communication.federation import open_communication_triad

    ports, failure, processes, ready_ports = parallel_backend_failure
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    with pytest.raises(BaseExceptionGroup) as caught:
        async with open_communication_triad(context, _prepare_workspace, ports=ports):
            pytest.fail("一个节点启动失败时不得进入业务阶段")

    assert caught.value.exceptions == (failure,)
    assert set(ready_ports) == {ports.port(0), ports.port(40)}
    assert len(processes) == 3
    assert all(process.poll() is not None for process in processes)


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

    with running_http_server(HTTPServer(("127.0.0.1", 0), Handler)) as server:
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
    # HTTPServer 由当前 pytest 进程持有；只替代退出观察，不伪造监听 PID。
    value.pid = os.getpid()
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
