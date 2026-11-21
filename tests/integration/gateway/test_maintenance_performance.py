"""真实 Gateway/Backend 上固定工作负载；采集进程独立硬超时60秒。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
    workspace_backend_id_for_root,
)


@pytest.fixture(scope="module")
def integration_backend_process(integration_workspace_root_path: str, integration_backend_port: int):
    handle = start_backend_process(workspace_root=integration_workspace_root_path, port=integration_backend_port,
        log_name="maintenance-backend", asgi_app="tests.integration.gateway.maintenance_app:app")
    try:
        yield handle.process
    finally:
        close_backend_process(handle)


def test_activity_prune_gateway_pagination_budget(request: pytest.FixtureRequest, integration_backend_process,
                                                 integration_backend_port: int, integration_workspace_root_path: str) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    port = integration_port_block_for_file(Path(request.node.path)).port(1)
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=f"http://127.0.0.1:{integration_backend_port}",
                                    port=port, boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir)
    try:
        url = f"http://127.0.0.1:{port}"
        workspace_id = workspace_backend_id_for_root(workspace)
        with httpx.Client(base_url=url, headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": workspace_id}, trust_env=False) as client:
            response = client.post("/api/v1/sessions", json={"title": "维护延迟有效会话"})
            response.raise_for_status()
            session_id = response.json()["data"]["session_id"]
        command = [sys.executable, "-m", "tests.integration.gateway.maintenance_performance", url, str(workspace), workspace_id,
                   session_id, str(context.artifacts_dir / "measurements.json")]
        (context.artifacts_dir / "command.json").write_text(json.dumps(command))
        with (context.artifacts_dir / "stdout.raw").open("w") as stdout, (context.artifacts_dir / "stderr.raw").open("w") as stderr:
            try:
                result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=60, check=False)
            except subprocess.TimeoutExpired:
                (context.artifacts_dir / "result.json").write_text(json.dumps({"exit_code": None, "timeout_seconds": 60}))
                raise
        (context.artifacts_dir / "result.json").write_text(json.dumps({"exit_code": result.returncode}))
        assert result.returncode == 0, (context.artifacts_dir / "stderr.raw").read_text()
    finally:
        close_gateway_process(gateway)
