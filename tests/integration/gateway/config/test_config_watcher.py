"""真实 Gateway 在拒绝坏配置后仍能观察下一次文件修复。"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@dataclass(frozen=True)
class WatchedGateway:
    client: httpx.Client
    config_path: Path
    artifacts: Path


@pytest.fixture
def watched_gateway(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Iterator[WatchedGateway]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    ports = integration_port_block_for_file(Path(request.node.path))
    with ExitStack() as resources:
        backend = start_backend_process(
            workspace_root=str(workspace), port=ports.port(0), log_name="config-watcher-backend",
        )
        resources.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=ports.port(1), artifacts_dir=context.artifacts_dir,
        )
        resources.callback(close_gateway_process, gateway)
        client = resources.enter_context(httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=10, trust_env=False,
        ))
        yield WatchedGateway(client, workspace.parent / "boxteam-home/config/gateway.jsonc", context.artifacts_dir)


def test_gateway_observes_repair_after_rejected_file(watched_gateway: WatchedGateway) -> None:
    case = watched_gateway
    observations: list[dict[str, object]] = []

    def status() -> dict[str, object]:
        response = case.client.get("/api/gateway/config/reload-status")
        response.raise_for_status()
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        result = response.json()["data"]
        observations.append(result)
        return result

    def wait_for(*, healthy: bool) -> dict[str, object]:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = status()
            if current["healthy"] is healthy:
                return current
            time.sleep(0.05)
        raise AssertionError(f"配置观察未收敛: healthy={healthy}, last={observations[-1]}")

    try:
        original = commentjson.loads(case.config_path.read_text(encoding="utf-8"))
        before = status()
        revision = before["revision"]
        assert isinstance(revision, str)
        assert before["healthy"] is True and before["state"] == "active"
        case.config_path.write_text("{ broken", encoding="utf-8")
        failed = wait_for(healthy=False)
        assert failed["reason"] == "invalid_config"
        assert failed["active_revision"] == before["active_revision"]
        assert failed["revision"] == revision
        assert failed["pending_revision"] is None
        assert "配置 JSONC 解析失败" in str(failed["last_error"])
        assert str(case.config_path) not in str(failed["last_error"])

        original["logging"]["runtime"]["level"] = "debug"
        case.config_path.write_text(json.dumps(original), encoding="utf-8")
        recovered = wait_for(healthy=True)
        assert recovered["state"] == "active" and recovered["last_error"] is None
        assert recovered["revision"] != revision
        assert recovered["active_revision"] != before["active_revision"]
        for endpoint in ("/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"):
            response = case.client.get(endpoint)
            response.raise_for_status()
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
        events = case.client.get("/api/gateway/config/events").raise_for_status().json()["data"]["events"]
        assert [event["result"] for event in events] == ["applied"]
        assert events[0]["active_revision"] == recovered["active_revision"]
        assert events[0]["applied_paths"] == ["/logging/runtime/level"]
    finally:
        (case.artifacts / "watcher-status.json").write_text(
            json.dumps(observations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
