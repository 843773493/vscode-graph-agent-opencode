"""独立 Gateway 与显式 HTTP 替身验证健康身份失败及恢复。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, closing
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.runtime.workspace_process import resolve_backend_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
)

health_stub_app = FastAPI()


@health_stub_app.get("/api/v1/health")
async def stub_health() -> object:
    return json.loads(Path(os.environ["BOXTEAM_TEST_HEALTH_RESPONSE"]).read_text())


@health_stub_app.get("/api/v1/config/reload-status")
async def stub_config() -> dict[str, object]:
    return {"data": {"healthy": True, "restart_required": False, "changed_sections": []}}


@health_stub_app.get("/api/v1/session-catalog/export")
async def stub_catalog() -> dict[str, object]:
    return {"data": {"revision": "health-stub-empty", "items": []}}


@pytest.fixture
def health_stub(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Iterator[tuple[TestRunContext, Path, str, int, int]]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    root = Path(integration_workspace_root_path)
    backend_id = resolve_backend_workspace_id(Path.cwd(), root)
    control = context.runtime_root_for_node(request.node.nodeid) / "health-response.json"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(json.dumps({"status": "ok", "workspace_id": backend_id}))
    ports = integration_port_block_for_file(Path(request.node.fspath))
    ensure_local_port_available(ports.port(0), service_name="健康协议替身")
    with ExitStack() as lifetime:
        log = lifetime.enter_context((context.artifacts_dir / "health-stub.log").open("a"))
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", f"{__name__}:health_stub_app", "--host", "127.0.0.1",
             "--port", str(ports.port(0)), "--log-level", "warning"],
            env={**os.environ, "BOXTEAM_TEST_HEALTH_RESPONSE": str(control)}, stdout=log, stderr=log,
        )
        lifetime.callback(terminate_process, process)
        wait_for_http_ok(f"http://127.0.0.1:{ports.port(0)}/api/v1/health", process)
        yield context, control, backend_id, ports.port(0), ports.port(1)


@pytest.mark.asyncio
async def test_health_identity_failure_does_not_publish_or_persist_response_values(
    health_stub: tuple[TestRunContext, Path, str, int, int],
) -> None:
    context, control, backend_id, backend_port, gateway_port = health_stub
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    evidence = []
    with ExitStack() as lifetime:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=f"http://127.0.0.1:{backend_port}",
            port=gateway_port, artifacts_dir=context.artifacts_dir,
        )
        lifetime.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            response = await client.get("/api/gateway/workspaces")
            assert response.status_code == 200, response.text
            item, = response.json()["data"]["items"]
            workspace_id = item["workspace_id"]
            assert item["status"] == "ready" and item["backend_workspace_id"] == backend_id
            expected = f"Workspace 健康响应 backend UUID 不匹配: expected={backend_id}"
            for invalid_id in (str(context.workspace_root / "private-health-marker"), "C:\\private-health-marker\\root", None):
                control.write_text(json.dumps({"status": "ok", "workspace_id": invalid_id}))
                listed = await client.get("/api/gateway/workspaces")
                assert listed.status_code == 200, listed.text
                assert listed.json()["request_id"] == listed.headers["X-Request-ID"]
                item, = listed.json()["data"]["items"]
                evidence.append({"invalid_id": invalid_id, "list": listed.json()})
                (context.artifacts_dir / "health-protocol-errors.json").write_text(json.dumps(evidence, indent=2))
                assert item["status"] == "offline"
                assert item["services"]["workspace_api"]["error"] == expected
                assert "private-health-marker" not in listed.text
                failed = await client.post(f"/api/gateway/workspaces/{workspace_id}/probe")
                assert failed.status_code == 502, failed.text
                assert failed.json()["detail"] == expected
                assert failed.headers["X-Request-ID"]
                assert "private-health-marker" not in failed.text
                unchecked = await client.get("/api/gateway/workspaces", params={"check_health": False})
                assert unchecked.status_code == 200, unchecked.text
                item, = unchecked.json()["data"]["items"]
                assert item["connection_error"] == expected
                state = context.boxteam_home / "state/gateway/gateway.sqlite"
                with closing(sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)) as connection:
                    row, = connection.execute(
                        "SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (workspace_id,),
                    ).fetchone()
                record = json.loads(row)
                assert record["backend_workspace_id"] == backend_id
                assert record["connection_error"] == expected
                evidence[-1].update({"probe": failed.json(), "unchecked": unchecked.json(), "persisted_error": record["connection_error"]})
            control.write_text(json.dumps({"status": "ok", "workspace_id": backend_id}))
            recovered = await client.post(f"/api/gateway/workspaces/{workspace_id}/probe")
            assert recovered.status_code == 200, recovered.text
            item, = recovered.json()["data"]["items"]
            assert item["status"] == "ready" and item["connection_error"] is None
            assert item["backend_workspace_id"] == backend_id
            evidence.append({"recovered": recovered.json()})
        log = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert "private-health-marker" in log
        (context.artifacts_dir / "health-protocol-errors.json").write_text(json.dumps(evidence, indent=2) + "\n")


@pytest.mark.asyncio
async def test_gateway_workspace_control_ignores_model_proxy_environment(
    health_stub: tuple[TestRunContext, Path, str, int, int],
) -> None:
    context, _, backend_id, backend_port, gateway_port = health_stub
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    # 代理地址故意不可用，NO_PROXY 为空；控制面不能依赖模型网络的代理配置。
    proxy_environment = {name: "http://127.0.0.1:1" for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    )}
    proxy_environment.update({"NO_PROXY": "", "no_proxy": ""})
    with ExitStack() as lifetime:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=f"http://127.0.0.1:{backend_port}",
            port=gateway_port, artifacts_dir=context.artifacts_dir, extra_env=proxy_environment,
        )
        lifetime.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=10, trust_env=False,
        ) as client:
            await acquire_gateway_guest(client)
            listed = await client.get("/api/gateway/workspaces")
            assert listed.status_code == 200, listed.text
            target, = listed.json()["data"]["items"]
            assert target["status"] == "ready" and target["backend_workspace_id"] == backend_id
            probe = await client.post(f"/api/gateway/workspaces/{target['workspace_id']}/probe")
            assert probe.status_code == 200, probe.text
            target, = probe.json()["data"]["items"]
            assert target["status"] == "ready" and target["connection_error"] is None
            assert target["backend_workspace_id"] == backend_id
            assert probe.json()["request_id"] == probe.headers["X-Request-ID"]
            (context.artifacts_dir / "proxy-environment-control.json").write_text(
                json.dumps({"list": listed.json(), "probe": probe.json()}, indent=2) + "\n",
                encoding="utf-8",
            )
