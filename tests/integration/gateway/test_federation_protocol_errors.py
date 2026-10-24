"""真实 Gateway 管理入口与独立远端 HTTP 替身的响应正文边界。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.path_utils import get_gateway_root
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.lifespan import lifespan
from app.gateway.registry import WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
)

CONNECTION_ID = "rgw_protocol_integration"
REMOTE_GATEWAY_ID = "gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PROJECTED_WORKSPACE_ID = "projected_protocol"
REMOTE_WORKSPACE_ID = "remote_protocol"
remote_stub_app = FastAPI()


@remote_stub_app.get("/api/gateway/health")
async def stub_health() -> dict[str, str]:
    return {"status": "ok"}


@remote_stub_app.get("/api/gateway/federation/directories")
@remote_stub_app.get("/api/gateway/federation/diagnostics")
@remote_stub_app.post("/api/gateway/workspaces/remote_protocol/runtime/{action}")
async def stub_management(request: Request) -> JSONResponse:
    control = Path(os.environ["BOXTEAM_TEST_FEDERATION_REPLY"])
    response = json.loads(control.read_text())
    with control.with_suffix(".requests.jsonl").open("a") as log:
        log.write(json.dumps({"path": request.url.path, "request_id": request.headers["X-Request-ID"]}) + "\n")
    return JSONResponse(content=response)


@asynccontextmanager
async def protocol_lifespan(application: FastAPI) -> AsyncIterator[None]:
    async with lifespan(application):
        # 装具只预置已连接远端及凭据；被测路由、HTTP 客户端与响应解析均为生产实现。
        FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
            connection_id=CONNECTION_ID, peer_gateway_id=load_or_create_gateway_id(get_gateway_root() / "identity.json"),
        )
        application.state.registry.upsert_remote_gateway(
            RemoteGatewayConnection(
                connection_id=CONNECTION_ID, name="远端协议装具", host="127.0.0.1", port=22,
                username="integration", private_key_path=None, ssh_config_host=None,
                remote_gateway_port=int(os.environ["BOXTEAM_TEST_FEDERATION_PORT"]),
                remote_gateway_id=REMOTE_GATEWAY_ID, protocol_version=1, source_owner="manual",
            ),
            runtime=WorkspaceRuntime(service_urls={
                "workspace_api": f"http://127.0.0.1:{os.environ['BOXTEAM_TEST_FEDERATION_PORT']}",
            }),
        )
        application.state.registry.upsert(WorkspaceTarget(
            workspace_id=PROJECTED_WORKSPACE_ID, name="远端协议工作区", root_path=None,
            backend_url=f"http://127.0.0.1:{os.environ['BOXTEAM_TEST_FEDERATION_PORT']}",
            connection_kind="remote_gateway", owner="remote_projection", managed=True, connection_id=CONNECTION_ID,
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            remote_gateway_connection_id=CONNECTION_ID, remote_workspace_id=REMOTE_WORKSPACE_ID,
            remote_service_names=("workspace_api",),
        ))
        yield


def create_gateway_app() -> FastAPI:
    from app.gateway.main import app

    app.router.lifespan_context = protocol_lifespan
    return app


@pytest.fixture
def remote_stub(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Iterator[tuple[TestRunContext, Path, int, int]]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    assert context.workspace_root == Path(integration_workspace_root_path)
    control = context.runtime_root_for_node(request.node.nodeid) / "federation-response.json"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text("{}")
    ports = integration_port_block_for_file(Path(request.node.fspath))
    ensure_local_port_available(ports.port(0), service_name="远端 Gateway HTTP 协议替身")
    with ExitStack() as processes:
        log = processes.enter_context((context.artifacts_dir / "remote-stub.log").open("a"))
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", f"{__name__}:remote_stub_app", "--host", "127.0.0.1",
             "--port", str(ports.port(0)), "--log-level", "warning"],
            env={**os.environ, "BOXTEAM_TEST_FEDERATION_REPLY": str(control)}, stdout=log, stderr=log,
        )
        processes.callback(terminate_process, process)
        wait_for_http_ok(f"http://127.0.0.1:{ports.port(0)}/api/gateway/health", process)
        yield context, control, ports.port(0), ports.port(1)


@pytest.mark.asyncio
async def test_invalid_envelope_and_explicit_raw_diagnostics_use_separate_response_boundaries(
    remote_stub: tuple[TestRunContext, Path, int, int],
) -> None:
    context, control, remote_port, gateway_port = remote_stub
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    paths = [str(context.workspace_root / "remote-private-marker"), r"C:\private\remote-private-marker",
             r"\\remote\share\remote-private-marker"]
    evidence: dict[str, object] = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=None,
            port=gateway_port, artifacts_dir=context.artifacts_dir,
            asgi_app=f"{__name__}:create_gateway_app",
            extra_env={"BOXTEAM_TEST_FEDERATION_PORT": str(remote_port)},
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=20,
        ) as client:
            for index, payload in enumerate(({"detail": paths}, {"data": paths}, paths)):
                control.write_text(json.dumps(payload))
                response = await client.get(
                    "/api/gateway/local-directories", params={"gateway_connection_id": CONNECTION_ID},
                    headers={"X-Request-ID": f"req_protocol_invalid_{index}"},
                )
                evidence[f"invalid-{index}"] = response.json()
                assert response.status_code == 502, response.text
                assert response.json()["detail"] == "远程 Gateway 响应缺少 data 对象: status=200"
                assert response.json()["request_id"] == response.headers["X-Request-ID"] == f"req_protocol_invalid_{index}"
                assert "remote-private-marker" not in response.text
            directory_uri = f"boxteam://gateway/{REMOTE_GATEWAY_ID}/resources/files/01900000000070008000000000000001"
            listing = {"name": "远端目录", "path": directory_uri, "home_path": directory_uri,
                       "entries": [], "limit": 120}
            control.write_text(json.dumps({"data": listing}))
            recovered = await client.get(
                "/api/gateway/local-directories", params={"gateway_connection_id": CONNECTION_ID},
                headers={"X-Request-ID": "req_protocol_recovered"},
            )
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["data"]["path"] == directory_uri
            evidence["recovered"] = recovered.json()
            tail = "\n".join(paths)
            diagnostics = {"gateway_id": REMOTE_GATEWAY_ID, "gateway_name": "原生远端",
                           "connection_kind": "local", "status": "ready", "checked_at": "2026-10-24T00:00:00Z",
                           "logs": [{"log_id": "gateway-stderr", "source": "gateway", "service": "gateway",
                                     "label": "原始服务日志", "status": "available", "tail": tail}]}
            control.write_text(json.dumps({"data": diagnostics}))
            raw = await client.get(
                "/api/gateway/diagnostics", params={"gateway_connection_id": CONNECTION_ID},
                headers={"X-Request-ID": "req_protocol_raw_logs"},
            )
            assert raw.status_code == 200, raw.text
            assert raw.json()["data"]["logs"][0]["tail"] == tail
            assert raw.json()["data"]["gateway_connection_id"] == CONNECTION_ID
            assert raw.json()["request_id"] == raw.headers["X-Request-ID"] == "req_protocol_raw_logs"
            evidence["raw-diagnostics"] = raw.json()
    log = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert "remote-private-marker" in log
    requests = [json.loads(line) for line in control.with_suffix(".requests.jsonl").read_text().splitlines()]
    assert [request["request_id"] for request in requests] == [
        "req_protocol_invalid_0", "req_protocol_invalid_1", "req_protocol_invalid_2",
        "req_protocol_recovered", "req_protocol_raw_logs",
    ]
    (context.artifacts_dir / "protocol-error-receipts.json").write_text(
        json.dumps({"responses": evidence, "remote_requests": requests}, ensure_ascii=False, indent=2) + "\n",
    )


@pytest.mark.asyncio
async def test_remote_lifecycle_protocol_failure_keeps_error_type_without_response_body(
    remote_stub: tuple[TestRunContext, Path, int, int],
) -> None:
    context, control, remote_port, gateway_port = remote_stub
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    paths = [str(context.workspace_root / "runtime-private-marker"), r"C:\private\runtime-private-marker",
             r"\\remote\share\runtime-private-marker"]
    evidence: dict[str, object] = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=None,
            port=gateway_port, artifacts_dir=context.artifacts_dir,
            asgi_app=f"{__name__}:create_gateway_app",
            extra_env={"BOXTEAM_TEST_FEDERATION_PORT": str(remote_port)},
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=20,
        ) as client:
            for action in ("start", "stop", "restart-safe", "restart-force"):
                for index, payload in enumerate(({"detail": paths}, {"data": paths}, paths)):
                    control.write_text(json.dumps(payload))
                    request_id = f"req_runtime_{action.replace('-', '_')}_{index}"
                    response = await client.post(
                        f"/api/gateway/workspaces/{PROJECTED_WORKSPACE_ID}/runtime/{action}",
                        headers={"X-Request-ID": request_id},
                    )
                    assert response.status_code == 500, response.text
                    assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
                    assert "runtime-private-marker" not in response.text, response.text
                    assert response.json()["message"] == "TypeError: Workspace API 生命周期响应缺少 data 对象: status=200"
                    evidence[request_id] = response.json()
            health = await client.get("/api/gateway/health")
            assert health.status_code == 200, health.text
    log = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert "runtime-private-marker" in log
    requests = [json.loads(line) for line in control.with_suffix(".requests.jsonl").read_text().splitlines()]
    assert len(requests) == 12
    assert [request["request_id"] for request in requests] == list(evidence)
    assert {request["path"] for request in requests} == {
        f"/api/gateway/workspaces/{REMOTE_WORKSPACE_ID}/runtime/{action}"
        for action in ("start", "stop", "restart-safe", "restart-force")
    }
    (context.artifacts_dir / "runtime-protocol-error-receipts.json").write_text(
        json.dumps({"responses": evidence, "remote_requests": requests}, ensure_ascii=False, indent=2) + "\n",
    )
