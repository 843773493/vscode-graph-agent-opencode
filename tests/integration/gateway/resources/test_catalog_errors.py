"""独立 Gateway 与下游 HTTP 替身验证资源查询错误及同入口恢复。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

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
    workspace_backend_id_for_root,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A

resource_stub_app = FastAPI()


@resource_stub_app.get("/api/v1/health")
async def stub_health(workspace_id: str | None = Header(default=None, alias="X-BoxTeam-Workspace-Id")) -> object:
    return {"status": "ok", "workspace_id": workspace_id or os.environ["BOXTEAM_TEST_CATALOG_OWNER"]}


@resource_stub_app.get("/api/v1/config/reload-status")
async def stub_config() -> object:
    return {"data": {"healthy": True, "restart_required": False, "changed_sections": []}}


@resource_stub_app.get("/api/v1/session-catalog/export")
async def stub_catalog() -> object:
    return {"data": {"revision": "resource-query-empty", "items": []}}


@resource_stub_app.get("/api/v1/resources")
def stub_resources(request: Request) -> JSONResponse:
    control = json.loads(Path(os.environ["BOXTEAM_TEST_CATALOG_CONTROL"]).read_text())
    owner = request.headers["X-BoxTeam-Workspace-Id"]
    with Path(control["requests_path"]).open("a") as output:
        output.write(json.dumps({"workspace_id": owner, "request_id": request.headers["X-Request-ID"]}) + "\n")
    resource = {
        "resource_id": f"browser-{owner}", "workspace_id": owner, "kind": "browser", "retention_scope": "workspace",
        "source_thread": {"session_id": SESSION_A, "thread_id": THREAD_A}, "thread_associations": [],
        "owner_revision": 3, "status": "running", "reconciliation_status": "verified",
        "unattached_since": None, "reclaim_at": None, "available_actions": ["stop", "delete"],
        "metadata": {"title": "查询用浏览器"}, "created_at": "2026-10-05T00:00:00Z",
        "updated_at": "2026-10-05T00:00:00Z", "error": None,
    }
    if owner == os.environ["BOXTEAM_TEST_CATALOG_OWNER"] and control["mode"] != "ready":
        if control["mode"] == "validation":
            return JSONResponse({"data": {"workspace_id": owner, "items": control["native_path"]}})
        if control["mode"] == "owner-envelope":
            return JSONResponse({"data": {"workspace_id": control["native_path"], "items": [resource]}})
        if control["mode"] == "owner-item":
            return JSONResponse({"data": {"workspace_id": owner, "items": [resource, {
                **resource, "resource_id": "browser-foreign", "workspace_id": control["native_path"],
            }]}})
        return JSONResponse({"message": control["native_path"]}, status_code=503 if control["mode"] == "http-status" else 200)
    return JSONResponse({"data": {"workspace_id": owner, "items": [resource]}})


@pytest.fixture
def catalog_upstream(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Iterator[tuple[TestRunContext, Path, str, int, int]]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    owner = workspace_backend_id_for_root(workspace)
    runtime = context.runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True, exist_ok=True)
    control = runtime / "resource-response.json"
    control.write_text(json.dumps({"mode": "ready", "requests_path": str(context.artifacts_dir / "upstream-requests.jsonl")}))
    ports = integration_port_block_for_file(Path(request.node.fspath))
    ensure_local_port_available(ports.port(0), service_name="资源目录 HTTP 替身")
    with ExitStack() as lifetime:
        log = lifetime.enter_context((context.artifacts_dir / "upstream.log").open("a"))
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", f"{__name__}:resource_stub_app", "--host", "127.0.0.1",
             "--port", str(ports.port(0)), "--log-level", "warning"],
            env={**os.environ, "BOXTEAM_TEST_CATALOG_OWNER": owner, "BOXTEAM_TEST_CATALOG_CONTROL": str(control)},
            stdout=log, stderr=log, text=True,
        )
        lifetime.callback(terminate_process, process)
        wait_for_http_ok(f"http://127.0.0.1:{ports.port(0)}/api/v1/health", process)
        yield context, control, owner, ports.port(0), ports.port(1)


@pytest.mark.asyncio
async def test_resource_catalog_errors_keep_healthy_scope_and_recover(
    catalog_upstream: tuple[TestRunContext, Path, str, int, int],
) -> None:
    context, control, failed_owner, upstream_port, gateway_port = catalog_upstream
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    sibling = context.workspace_root / "catalog-sibling"
    sibling.mkdir()
    sibling_owner = workspace_backend_id_for_root(sibling)
    evidence = []
    with ExitStack() as lifetime:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=f"http://127.0.0.1:{upstream_port}",
            port=gateway_port, artifacts_dir=context.artifacts_dir,
        )
        lifetime.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            await acquire_gateway_guest(client)
            added = await client.post("/api/gateway/workspaces/local", json={
                "root_path": str(sibling), "name": "健康工作区", "backend_url": f"http://127.0.0.1:{upstream_port}",
            })
            assert added.status_code == 200, added.text
            targets = {item["backend_workspace_id"]: item for item in added.json()["data"]["items"]}
            assert set(targets) == {failed_owner, sibling_owner}
            state = json.loads(control.read_text())
            for mode in ("envelope", "validation", "http-status", "owner-envelope", "owner-item"):
                for index, native_path in enumerate(("/private/catalog-marker/state.sqlite", r"C:\private\catalog-marker\state.sqlite", r"\\server\catalog-marker\state.sqlite", sibling_owner)):
                    state.update(mode=mode, native_path=native_path)
                    control.write_text(json.dumps(state))
                    request_id = f"catalog-{mode}-{index}"
                    failed = await client.get("/api/gateway/resources", headers={"X-Request-ID": request_id})
                    assert failed.status_code == 200, failed.text
                    body = failed.json()
                    assert body["request_id"] == failed.headers["X-Request-ID"] == request_id
                    assert "catalog-marker" not in failed.text
                    item, = body["data"]["items"]
                    assert item["workspace_id"] == targets[sibling_owner]["workspace_id"]
                    assert item["resource"]["workspace_id"] == sibling_owner
                    error, = body["data"]["errors"]
                    assert error["scope_key"] == targets[failed_owner]["workspace_id"]
                    assert error["label"] == f"本机 Gateway · {targets[failed_owner]['name']}"
                    error_type = {"envelope": "ValueError", "validation": "ValidationError", "http-status": "HTTPStatusError", "owner-envelope": "ValueError", "owner-item": "ValueError"}[mode]
                    assert error["message"] == f"工作区资源目录读取失败: {error_type}" + (", status=503" if mode == "http-status" else "")
                    observed = [json.loads(line) for line in Path(state["requests_path"]).read_text().splitlines()]
                    assert {row["workspace_id"] for row in observed if row["request_id"] == request_id} == {failed_owner, sibling_owner}
                    evidence.append(body)
            state["mode"] = "ready"
            control.write_text(json.dumps(state))
            recovered = await client.get("/api/gateway/resources")
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["data"]["errors"] == []
            assert {item["resource"]["workspace_id"] for item in recovered.json()["data"]["items"]} == {failed_owner, sibling_owner}
            evidence.append(recovered.json())
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert "catalog-marker" in logs and "Traceback" in logs
        assert "catalog-validation-1" in logs
        (context.artifacts_dir / "catalog-errors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
