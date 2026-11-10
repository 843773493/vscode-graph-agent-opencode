"""真实 Gateway 管理入口与独立远端 HTTP 替身的响应正文边界。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.path_utils import get_gateway_root
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.lifespan import lifespan
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
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
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
SIBLING_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"
remote_stub_app = FastAPI()


@remote_stub_app.get("/api/gateway/health")
async def stub_health() -> dict[str, str]:
    return {"status": "ok"}


@remote_stub_app.get("/api/gateway/federation/directories")
@remote_stub_app.get("/api/gateway/federation/diagnostics")
@remote_stub_app.get("/api/gateway/federation/managed-workspaces")
@remote_stub_app.post(f"/api/gateway/workspaces/{WORKSPACE_ID}/runtime/{{action}}")
@remote_stub_app.post("/api/gateway/federation/managed-workspaces")
@remote_stub_app.delete("/api/gateway/federation/managed-workspaces/{workspace_id}")
async def stub_management(request: Request) -> JSONResponse:
    control = Path(os.environ["BOXTEAM_TEST_FEDERATION_REPLY"])
    response = json.loads(control.read_text())
    with control.with_suffix(".requests.jsonl").open("a") as log:
        log.write(json.dumps({"path": request.url.path, "request_id": request.headers["X-Request-ID"],
                              "target_gateway_id": request.headers.get(TARGET_GATEWAY_ID_HEADER)}) + "\n")
    return JSONResponse(content=response["body"], status_code=response["status"])


@asynccontextmanager
async def protocol_lifespan(application: FastAPI) -> AsyncIterator[dict[str, Callable[[], Awaitable[None]] | None]]:
    async with lifespan(application) as state:
        # 装具只预置已连接远端及凭据；被测路由、HTTP 客户端与响应解析均为生产实现。
        FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
            connection_id=CONNECTION_ID, peer_gateway_id=load_or_create_gateway_id(get_gateway_root() / "identity.json"),
        )
        connection = RemoteGatewayConnection(
            connection_id=CONNECTION_ID, name="远端协议装具", host="127.0.0.1", port=22,
            username="integration", private_key_path=None, ssh_config_host=None,
            remote_gateway_port=int(os.environ["BOXTEAM_TEST_FEDERATION_PORT"]),
            remote_gateway_id=REMOTE_GATEWAY_ID, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
        )
        application.state.registry.apply_remote_projection_snapshot(
            connection=connection,
            projections=(WorkspaceTarget(
                gateway_id=REMOTE_GATEWAY_ID, workspace_id=WORKSPACE_ID, name="远端协议工作区", root_path=None,
                backend_url=f"http://127.0.0.1:{os.environ['BOXTEAM_TEST_FEDERATION_PORT']}",
                connection_kind="remote_gateway", owner="remote_projection", managed=True,
                remote_gateway_connection_ids=(CONNECTION_ID,), remote_service_names=("workspace_api",),
            ),),
            runtime=WorkspaceRuntime(service_urls={
                "workspace_api": f"http://127.0.0.1:{os.environ['BOXTEAM_TEST_FEDERATION_PORT']}",
            }),
        )
        yield state


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
    control.write_text(json.dumps({"status": 200, "body": {}}))
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
@pytest.mark.parametrize("operation", ["create", "directory", "list", "remove"])
async def test_remote_management_preserves_http_rejection_without_copying_remote_body(
    remote_stub: tuple[TestRunContext, Path, int, int], operation: str,
) -> None:
    context, control, remote_port, gateway_port = remote_stub
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    evidence = []
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
            for status, location in [(400, "/private/create-reply"), (404, r"C:\private\create-reply"),
                                     (503, r"\\server\private\create-reply")]:
                control.write_text(json.dumps({"status": status, "body": {"detail": location}}))
                request_id = f"req_remote_{operation}_{status}"
                if operation == "create":
                    response = await client.post(
                        "/api/gateway/managed-workspaces", headers={"X-Request-ID": request_id},
                        json={"gateway_connection_id": CONNECTION_ID, "root_path": "/selected"},
                    )
                    prefix = "工作区创建失败: stage=workspace_create"
                elif operation == "directory":
                    response = await client.get(
                        "/api/gateway/local-directories", headers={"X-Request-ID": request_id},
                        params={"gateway_connection_id": CONNECTION_ID, "path": "/selected"},
                    )
                    prefix = "目录浏览失败: stage=directory_browse"
                else:
                    response = await client.request(
                        "GET" if operation == "list" else "DELETE",
                        "/api/gateway/managed-workspaces" + (f"/{WORKSPACE_ID}" if operation == "remove" else ""),
                        headers={"X-Request-ID": request_id}, params={"gateway_connection_id": CONNECTION_ID},
                    )
                    prefix = "工作区清单失败: stage=workspace_list" if operation == "list" else "工作区删除失败: stage=workspace_remove"
                assert response.status_code == (status if status < 500 else 502), response.text
                assert response.json()["detail"] == f"{prefix}, error=HTTPStatusError"
                assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
                assert "create-reply" not in response.text
                evidence.append(response.json())
    requests = [json.loads(line) for line in control.with_suffix(".requests.jsonl").read_text().splitlines()]
    assert [request["request_id"] for request in requests] == [f"req_remote_{operation}_{status}" for status in (400, 404, 503)]
    (context.artifacts_dir / f"{operation}-error-receipts.json").write_text(
        json.dumps({"responses": evidence, "remote_requests": requests}, ensure_ascii=False, indent=2) + "\n",
    )


@pytest.mark.asyncio
async def test_remote_removal_conflict_preserves_complete_blocker_and_rejects_malformed_body(
    remote_stub: tuple[TestRunContext, Path, int, int],
) -> None:
    context, control, remote_port, gateway_port = remote_stub
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    blocked = {
        "gateway_id": REMOTE_GATEWAY_ID, "workspace_id": WORKSPACE_ID, "backend_generation": "generation_original",
        "affected_workspace_ids": [WORKSPACE_ID, SIBLING_WORKSPACE_ID], "status": "blocked",
        "blockers": [{"gateway_id": REMOTE_GATEWAY_ID, "kind": "job", "workspace_id": SIBLING_WORKSPACE_ID, "resource_id": "job_original",
                      "session_id": "session_original", "status": "running", "detail": "原执行仍在运行"}],
        "workspaces": {"active_workspace": None, "items": []},
    }
    evidence = []
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=None, port=gateway_port,
            artifacts_dir=context.artifacts_dir, asgi_app=f"{__name__}:create_gateway_app",
            extra_env={"BOXTEAM_TEST_FEDERATION_PORT": str(remote_port)},
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=20) as client:
            for index, body in enumerate([{"detail": blocked}, {"detail": "/private/removal-reply"}, [r"C:\private\removal-reply"]]):
                control.write_text(json.dumps({"status": 409, "body": body}))
                request_id = f"req_removal_conflict_{index}"
                response = await client.delete(
                    f"/api/gateway/managed-workspaces/{WORKSPACE_ID}", params={"gateway_connection_id": CONNECTION_ID},
                    headers={"X-Request-ID": request_id},
                )
                assert response.status_code == 409, response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
                assert response.json()["detail"] == (
                    blocked if index == 0 else "工作区删除失败: stage=workspace_remove, error=HTTPStatusError"
                )
                assert "removal-reply" not in response.text
                evidence.append(response.json())
            workspaces = await client.get("/api/gateway/workspaces")
            assert workspaces.status_code == 200, workspaces.text
            projected, = [item for item in workspaces.json()["data"]["items"]
                          if (item["gateway_id"], item["workspace_id"]) == (REMOTE_GATEWAY_ID, WORKSPACE_ID)]
            assert projected["backend_url"] == f"http://127.0.0.1:{remote_port}"
    requests = [json.loads(line) for line in control.with_suffix(".requests.jsonl").read_text().splitlines()]
    assert [request["request_id"] for request in requests] == [f"req_removal_conflict_{index}" for index in range(3)]
    log = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert "stage=workspace_remove_response, error=ValidationError" in log
    assert "/private/removal-reply" in log and "stage=workspace_remove_response, error=TypeError" in log
    (context.artifacts_dir / "removal-conflict-receipts.json").write_text(
        json.dumps({"responses": evidence, "remote_requests": requests}, ensure_ascii=False, indent=2) + "\n",
    )


@pytest.mark.skipif(os.name == "nt", reason="此项核真实POSIX目录读取拒绝；Windows ACL另行验收")
@pytest.mark.asyncio
async def test_native_directory_permission_failure_logs_original_and_retries_same_binding(
    integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    assert context.workspace_root == Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    directory = context.workspace_root / "private-directory-read"
    directory.mkdir()
    (directory / "child").mkdir()
    original_mode = directory.stat().st_mode
    port = integration_port_block_for_file(Path(__file__)).port(1)
    receipts = []
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=None,
            port=port, artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        store = FederationCredentialStore(storage_path=context.boxteam_home / "state/gateway/credentials/federation.json")
        credential = store.issue(connection_id="rgw_directory_read", peer_gateway_id="gateway_peer")
        cases = [
            ("local-directories", LOCAL_TOKEN_HEADERS),
            ("federation/directories", {"X-BoxTeam-Federation-Token": credential.token}),
        ]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
            directory.chmod(0o000)
            try:
                if os.access(directory, os.R_OK):
                    pytest.skip("当前用户不受POSIX目录读取权限限制")
                for index, (endpoint, headers) in enumerate(cases):
                    request_id = f"req_directory_denied_{index}"
                    response = await client.get(
                        f"/api/gateway/{endpoint}", params={"path": str(directory)},
                        headers={**headers, "X-Request-ID": request_id},
                    )
                    assert response.status_code == 403, response.text
                    assert response.json()["detail"] == "目录枚举失败: stage=directory_scan, error=PermissionError, errno=13"
                    assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
                    assert str(directory) not in response.text
                    receipts.append(response.json())
            finally:
                directory.chmod(original_mode)
            bindings = []
            for index, (endpoint, headers) in enumerate(cases):
                response = await client.get(
                    f"/api/gateway/{endpoint}", params={"path": str(directory)},
                    headers={**headers, "X-Request-ID": f"req_directory_recovered_{index}"},
                )
                assert response.status_code == 200, response.text
                assert [entry["name"] for entry in response.json()["data"]["entries"]] == ["child"]
                assert str(directory) not in response.text
                bindings.append(response.json()["data"]["path"])
                receipts.append(response.json())
            assert bindings[0] == bindings[1] and bindings[0].startswith("boxteam://gateway/")
    assert not (directory / ".boxteam").exists()
    logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert str(directory) in logs and "PermissionError: [Errno 13]" in logs
    for index in (0, 1):
        assert f'request_id="req_directory_denied_{index}"' in logs
    (context.artifacts_dir / "directory-permission-receipts.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2) + "\n",
    )


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
                control.write_text(json.dumps({"status": 200, "body": payload}))
                response = await client.get(
                    "/api/gateway/local-directories", params={"gateway_connection_id": CONNECTION_ID},
                    headers={"X-Request-ID": f"req_protocol_invalid_{index}"},
                )
                evidence[f"invalid-{index}"] = response.json()
                assert response.status_code == 502, response.text
                assert response.json()["detail"] == "目录浏览失败: stage=directory_browse, error=RuntimeError"
                assert response.json()["request_id"] == response.headers["X-Request-ID"] == f"req_protocol_invalid_{index}"
                assert "remote-private-marker" not in response.text
            directory_uri = f"boxteam://gateway/{REMOTE_GATEWAY_ID}/resources/files/01900000000070008000000000000001"
            listing = {"name": "远端目录", "path": directory_uri, "home_path": directory_uri,
                       "entries": [], "limit": 120}
            control.write_text(json.dumps({"status": 200, "body": {"data": listing}}))
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
            control.write_text(json.dumps({"status": 200, "body": {"data": diagnostics}}))
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
    assert "远程 Gateway 响应缺少 data 对象: status=200" in log
    requests = [json.loads(line) for line in control.with_suffix(".requests.jsonl").read_text().splitlines()]
    assert [request["request_id"] for request in requests] == [
        "req_protocol_invalid_0", "req_protocol_invalid_1", "req_protocol_invalid_2",
        "req_protocol_recovered", "req_protocol_raw_logs",
    ]
    (context.artifacts_dir / "protocol-error-receipts.json").write_text(
        json.dumps({"responses": evidence, "remote_requests": requests}, ensure_ascii=False, indent=2) + "\n",
    )


@pytest.mark.asyncio
async def test_remote_diagnostics_rejections_do_not_expand_the_raw_log_exception(
    remote_stub: tuple[TestRunContext, Path, int, int],
) -> None:
    context, control, remote_port, gateway_port = remote_stub
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    paths = [str(context.workspace_root / "diagnostics-private-marker"),
             r"C:\private\diagnostics-private-marker", r"\\remote\private\diagnostics-private-marker"]
    tail = "\n".join(paths) + "\n" + "x" * 2048 + "diagnostics-original-end"
    diagnostics = {
        "gateway_id": REMOTE_GATEWAY_ID, "gateway_name": "原生远端", "connection_kind": "local",
        "status": "ready", "checked_at": "2026-10-29T00:00:00Z",
        "logs": [{"log_id": "gateway-stderr", "source": "gateway", "service": "gateway",
                  "label": "原始日志", "status": "available", "tail": tail}],
    }
    receipts = []
    expected_requests = []
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
            params = {"gateway_connection_id": CONNECTION_ID, "workspace_id": WORKSPACE_ID}
            denied = await client.get("/api/gateway/diagnostics", params=params, headers={"X-Local-Token": "invalid"})
            assert denied.status_code == 401, denied.text
            assert not control.with_suffix(".requests.jsonl").exists()
            for index, path in enumerate(paths):
                for failure in ("http-403", "http-503", "validation"):
                    status = int(failure.removeprefix("http-")) if failure != "validation" else 200
                    body = {"detail": path + "x" * 2048 + "diagnostics-original-end"} if status != 200 else {
                        "data": {**diagnostics, "logs": path},
                    }
                    control.write_text(json.dumps({"status": status, "body": body}))
                    request_id = f"req_diagnostics_{index}_{failure}"
                    response = await client.get(
                        "/api/gateway/diagnostics", params=params, headers={"X-Request-ID": request_id},
                    )
                    assert response.status_code == (502 if status != 200 else 400), response.text
                    assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
                    assert "diagnostics-private-marker" not in response.text
                    assert "diagnostics-original-end" not in response.text
                    expected = "error=HTTPStatusError" if status != 200 else "error=ValidationError"
                    assert expected in response.json()["detail"]
                    if status != 200:
                        assert f"status={status}" in response.json()["detail"]
                    receipts.append(response.json())
                    expected_requests.append(request_id)

                    control.write_text(json.dumps({"status": 200, "body": {"data": diagnostics}}))
                    recovered_id = request_id + "_recovered"
                    recovered = await client.get(
                        "/api/gateway/diagnostics", params=params, headers={"X-Request-ID": recovered_id},
                    )
                    assert recovered.status_code == 200, recovered.text
                    assert recovered.json()["request_id"] == recovered.headers["X-Request-ID"] == recovered_id
                    data = recovered.json()["data"]
                    assert data["logs"][0]["tail"] == tail
                    assert data["gateway_connection_id"] == CONNECTION_ID
                    assert data["selected_workspace_id"] == WORKSPACE_ID
                    assert data["connection_kind"] == "remote_gateway"
                    expected_requests.append(recovered_id)
                    receipts.append(recovered.json())
    logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert "diagnostics-original-end" in logs and "diagnostics-private-marker" in logs
    requests = [json.loads(line) for line in control.with_suffix(".requests.jsonl").read_text().splitlines()]
    assert [entry["request_id"] for entry in requests] == expected_requests
    for request_id in expected_requests[::2]:
        assert request_id in logs
    (context.artifacts_dir / "diagnostics-error-receipts.json").write_text(
        json.dumps({"responses": receipts, "remote_requests": requests}, ensure_ascii=False, indent=2) + "\n",
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
                    control.write_text(json.dumps({"status": 200, "body": payload}))
                    request_id = f"req_runtime_{action.replace('-', '_')}_{index}"
                    response = await client.post(
                        f"/api/gateway/workspaces/{WORKSPACE_ID}/runtime/{action}",
                        headers={"X-Request-ID": request_id, TARGET_GATEWAY_ID_HEADER: REMOTE_GATEWAY_ID},
                    )
                    assert response.status_code == 500, response.text
                    assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
                    assert "runtime-private-marker" not in response.text, response.text
                    assert response.json()["message"] == "请求处理失败: stage=http_request, error=TypeError"
                    evidence[request_id] = response.json()
            health = await client.get("/api/gateway/health")
            assert health.status_code == 200, health.text
    log = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert "runtime-private-marker" in log
    assert "TypeError: Workspace API 生命周期响应缺少 data 对象: status=200" in log
    requests = [json.loads(line) for line in control.with_suffix(".requests.jsonl").read_text().splitlines()]
    assert len(requests) == 12
    assert {request["target_gateway_id"] for request in requests} == {REMOTE_GATEWAY_ID}
    assert [request["request_id"] for request in requests] == list(evidence)
    assert {request["path"] for request in requests} == {
        f"/api/gateway/workspaces/{WORKSPACE_ID}/runtime/{action}"
        for action in ("start", "stop", "restart-safe", "restart-force")
    }
    (context.artifacts_dir / "runtime-protocol-error-receipts.json").write_text(
        json.dumps({"responses": evidence, "remote_requests": requests}, ensure_ascii=False, indent=2) + "\n",
    )


@pytest.mark.skipif(os.name == "nt", reason="此项核真实POSIX权限；Windows ACL另有专门验收")
@pytest.mark.asyncio
async def test_federation_credential_permission_failure_keeps_request_identity_and_can_retry(
    integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    assert context.workspace_root == Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    port = integration_port_block_for_file(Path(__file__)).port(1)
    credential_path = context.workspace_root.parent / "boxteam-home/state/gateway/credentials/federation.json"
    receipts: dict[str, object] = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=None,
            port=port, artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        # 仅通过原凭据owner签发认证前置；HTTP认证、清单与失败重试均走独立Gateway。
        store = FederationCredentialStore(storage_path=credential_path)
        credential = store.issue(connection_id="rgw_permission_read", peer_gateway_id="gateway_peer")
        original = credential_path.read_bytes()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
            headers = {"X-BoxTeam-Federation-Token": credential.token, "X-Request-ID": "req_credential_reject"}
            credential_path.chmod(0o644)
            try:
                failed = await client.get("/api/gateway/federation/workspaces", headers=headers)
                assert failed.status_code == 401, failed.text
                assert failed.json()["request_id"] == failed.headers["X-Request-ID"] == headers["X-Request-ID"]
                assert "artifact=federation_credentials" in failed.json()["detail"]
                assert "stage=permissions" in failed.json()["detail"]
                assert str(credential_path) not in failed.text
                assert credential.token not in failed.text
                assert credential_path.read_bytes() == original
                receipts["rejected"] = failed.json()
            finally:
                credential_path.chmod(0o600)
            headers["X-Request-ID"] = "req_credential_repaired"
            recovered = await client.get("/api/gateway/federation/workspaces", headers=headers)
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["request_id"] == recovered.headers["X-Request-ID"] == headers["X-Request-ID"]
            assert recovered.json()["data"]["gateway_id"] == load_or_create_gateway_id(credential_path.parent.parent / "identity.json")
            assert store.verify(credential.token) == credential
            assert credential_path.read_bytes() == original
            receipts["recovered"] = recovered.json()
            denied = await client.get("/api/gateway/federation/workspaces", headers={"X-BoxTeam-Federation-Token": "invalid"})
            assert denied.status_code == 401, denied.text
            assert denied.json()["detail"] == "无效或已过期的 Gateway 联邦凭据"
    log = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert str(credential_path) in log
    assert "req_credential_reject" in log
    assert credential.token not in log
    (context.artifacts_dir / "credential-read-receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_gateway_identity_failure_keeps_original_identity_and_request_until_repaired(
    integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    assert context.workspace_root == Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    port = integration_port_block_for_file(Path(__file__)).port(1)
    identity_path = context.workspace_root.parent / "boxteam-home/state/gateway/identity.json"
    receipts: dict[str, object] = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=None,
            port=port, artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        original = identity_path.read_bytes()
        gateway_id = json.loads(original)["gateway_id"]
        credential_path = identity_path.parent / "credentials/federation.json"
        store = FederationCredentialStore(storage_path=credential_path)
        credential = store.issue(connection_id="rgw_identity_read", peer_gateway_id=gateway_id)
        credential_bytes = credential_path.read_bytes()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
            for kind, damaged, stage in (
                ("value", json.dumps({"gateway_id": str(identity_path)}).encode(), "validate"),
                ("json", b'{"gateway_id":', "decode"),
                ("utf8", str(identity_path).encode() + b"\xff", "decode"),
                ("directory", b"", "read"),
            ):
                request_id = f"req_identity_{kind}"
                headers = {"X-BoxTeam-Federation-Token": credential.token, "X-Request-ID": request_id}
                if kind == "directory":
                    identity_path.unlink()
                    identity_path.mkdir()
                else:
                    identity_path.write_bytes(damaged)
                try:
                    failed = await client.get("/api/gateway/federation/workspaces", headers=headers)
                    assert failed.status_code == 500, failed.text
                    assert failed.json()["request_id"] == failed.headers["X-Request-ID"] == request_id
                    error_type = {"value": "ValueError", "json": "JSONDecodeError", "utf8": "UnicodeDecodeError", "directory": "IsADirectoryError"}[kind]
                    expected = f"请求处理失败: stage=http_request, error={error_type}"
                    if kind == "directory":
                        expected += ", errno=21"
                    elif kind == "json":
                        expected += ", lineno=1, colno=15, pos=14"
                    assert failed.json()["message"] == expected
                    diagnostics = (context.artifacts_dir / "gateway.stderr.log").read_text()
                    assert f"artifact=gateway_identity, stage={stage}, error=" in diagnostics
                    assert f"request_id={request_id}" in diagnostics
                    assert str(identity_path) not in failed.text
                    assert credential.token not in failed.text
                    if kind == "directory":
                        assert identity_path.is_dir()
                    else:
                        assert identity_path.read_bytes() == damaged
                    receipts[kind] = failed.json()
                finally:
                    if kind == "directory":
                        identity_path.rmdir()
                    identity_path.write_bytes(original)
                headers["X-Request-ID"] = f"{request_id}_repaired"
                recovered = await client.get("/api/gateway/federation/workspaces", headers=headers)
                assert recovered.status_code == 200, recovered.text
                assert recovered.json()["request_id"] == recovered.headers["X-Request-ID"] == headers["X-Request-ID"]
                assert recovered.json()["data"]["gateway_id"] == gateway_id
                assert identity_path.read_bytes() == original
                assert credential_path.read_bytes() == credential_bytes
                assert store.verify(credential.token) == credential
                receipts[f"{kind}_repaired"] = recovered.json()
    log = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert str(identity_path) in log
    assert all(f"req_identity_{kind}" in log for kind in ("value", "json", "utf8", "directory"))
    assert credential.token not in log
    (context.artifacts_dir / "identity-error-receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.skipif(os.name == "nt", reason="此项核真实POSIX目录权限；Windows ACL另有专门验收")
@pytest.mark.asyncio
async def test_device_credential_publish_failure_keeps_authorization_until_explicit_retry(
    integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    assert context.workspace_root == Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    port = integration_port_block_for_file(Path(__file__)).port(1)
    credential_path = context.workspace_root.parent / "boxteam-home/state/gateway/credentials/federation.json"
    receipts: dict[str, object] = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=context.workspace_root, default_backend_url=None,
            port=port, artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=LOCAL_TOKEN_HEADERS, timeout=20) as client:
            body = {"device_name": "原设备", "gateway_url": f"http://127.0.0.1:{port}"}
            created = await client.post("/api/gateway/device-connections", json=body)
            assert created.status_code == 200, created.text
            original_connection = created.json()["data"]["connection"]
            original_token = created.json()["data"]["connection_info"]["federation_token"]
            original = credential_path.read_bytes()
            mode = credential_path.parent.stat().st_mode & 0o777
            credential_path.parent.chmod(0o500)
            try:
                for method, endpoint, payload in (
                    ("POST", "/api/gateway/device-connections", {**body, "device_name": "新设备"}),
                    ("DELETE", f"/api/gateway/device-connections/{original_connection['connection_id']}", None),
                ):
                    request_id = f"req_credential_publish_{method.lower()}"
                    failed = await client.request(method, endpoint, json=payload, headers={"X-Request-ID": request_id})
                    assert failed.status_code == 500, failed.text
                    assert failed.json()["request_id"] == failed.headers["X-Request-ID"] == request_id
                    assert failed.json()["message"] == "请求处理失败: stage=http_request, error=PermissionError, errno=13"
                    diagnostics = (context.artifacts_dir / "gateway.stderr.log").read_text()
                    assert "artifact=federation_credentials, stage=create_temp, error=PermissionError" in diagnostics
                    assert f"request_id={request_id}" in diagnostics
                    assert str(credential_path.parent) not in failed.text
                    assert original_token not in failed.text
                    assert credential_path.read_bytes() == original
                    receipts[request_id] = failed.json()
                listed = await client.get("/api/gateway/device-connections")
                assert listed.status_code == 200, listed.text
                assert listed.json()["data"]["items"] == [original_connection]
                authenticated = await client.get("/api/gateway/federation/workspaces", headers={"X-Local-Token": "", "X-BoxTeam-Federation-Token": original_token})
                assert authenticated.status_code == 200, authenticated.text
            finally:
                credential_path.parent.chmod(mode)
            added = await client.post("/api/gateway/device-connections", json={**body, "device_name": "新设备"})
            assert added.status_code == 200, added.text
            new_connection = added.json()["data"]["connection"]
            new_token = added.json()["data"]["connection_info"]["federation_token"]
            revoked = await client.delete(f"/api/gateway/device-connections/{original_connection['connection_id']}")
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["data"]["items"] == [new_connection]
            for token, status in ((original_token, 401), (new_token, 200)):
                response = await client.get("/api/gateway/federation/workspaces", headers={"X-Local-Token": "", "X-BoxTeam-Federation-Token": token})
                assert response.status_code == status, response.text
            receipts["revoked"] = revoked.json()
    log = (context.artifacts_dir / "gateway.stderr.log").read_text()
    assert str(credential_path) in log
    assert "req_credential_publish_post" in log and "req_credential_publish_delete" in log
    assert original_token not in log and new_token not in log
    (context.artifacts_dir / "credential-publish-receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n")
