"""不构建界面，核真实 Gateway 及其 Python／Node 服务的日志身份交付。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app.runtime.log_file_identity import runtime_log_path_identity
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)


@pytest.fixture
def logging_gateway(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Iterator[tuple[httpx.Client, TestRunContext]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    gateway = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path), default_backend_url=None,
        port=integration_port_block_for_file(Path(request.node.path)).port(0),
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=10, trust_env=False,
        ) as client:
            yield client, context
    finally:
        close_gateway_process(gateway)


def test_managed_backend_logging_control_starts_without_workbench(
    logging_gateway: tuple[httpx.Client, TestRunContext],
) -> None:
    client, context = logging_gateway
    response = client.get("/api/gateway/workspaces", headers={"X-Request-ID": "logging-startup"})
    response.raise_for_status()
    envelope = response.json()
    assert response.headers["X-Request-ID"] == envelope["request_id"] == "logging-startup"
    workspace, = envelope["data"]["items"]
    assert workspace["status"] == "ready"
    observed = {}
    try:
        base = workspace["services"]["workspace_api"]["local_url"].rstrip("/")
        reply = client.get(f"{base}/api/v1/internal/runtime-logging")
        observed["backend"] = {"status": reply.status_code, "body": reply.json()}
        assert reply.status_code == 200, observed["backend"]
        state = reply.json()["data"]
        assert state["service"] == "backend" and state["pid"] > 0
        sink = state["log_sink"]
        assert sink["role"] == "backend" and sink["source"] == "gateway"
        assert runtime_log_path_identity(Path(sink["path"])) == (sink["device"], sink["inode"])
        record = json.loads(Path(sink["path"] + ".owner.json").read_text(encoding="utf-8"))
        assert all(record[key] == value for key, value in sink.items())
        assert record["pid"] == state["pid"]
    finally:
        (context.artifacts_dir / "runtime-logging-startup.json").write_text(
            json.dumps(observed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
