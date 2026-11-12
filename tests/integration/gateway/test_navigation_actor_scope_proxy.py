"""经真实 Gateway 与独立 FastAPI 下游验证导航 enqueue/status 的认证 scope。"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import httpx
import pytest

from app.core.identifier import create_prefixed_id
from app.core.workspace_identity import read_workspace_id
from app.gateway.credentials import FederationCredential, FederationCredentialStore
from app.gateway.proxy_upstream import load_proxy_gateway_id
from app.services.business.session_context_resource import (
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    ensure_local_port_available,
    resolve_workspace_python_executable,
    terminate_process,
    wait_for_http_ok,
)

_OPERATION_ID = create_prefixed_id("op")
_ALICE_PRIVATE_OPERATION_ID = create_prefixed_id("op")


@dataclass(frozen=True, slots=True)
class _NavigationScopeBackendProcess:
    process: subprocess.Popen[str]
    stdout_file: IO[str]
    stderr_file: IO[str]
    port: int


@dataclass(frozen=True, slots=True)
class _NavigationScopeProxyRuntime:
    workspace_id: str
    backend_port: int
    gateway: GatewayProcess
    peer_credential: FederationCredential


def _close_navigation_scope_backend(
    handle: _NavigationScopeBackendProcess,
) -> None:
    errors: list[BaseException] = []
    for cleanup in (
        lambda: terminate_process(handle.process),
        handle.stdout_file.close,
        handle.stderr_file.close,
    ):
        try:
            cleanup()
        except BaseException as error:  # noqa: BLE001 - 中断也须释放其余资源，再报告全部错误。
            errors.append(error)
    if errors:
        raise BaseExceptionGroup("导航 scope 测试进程及日志清理失败", errors)


def _start_navigation_scope_backend(
    *,
    workspace_root: Path,
    port: int,
) -> _NavigationScopeBackendProcess:
    ensure_local_port_available(port, service_name="导航 scope 下游后端")
    project_root = Path.cwd().resolve()
    python_executable = resolve_workspace_python_executable(project_root)
    boxteam_home = workspace_root.resolve().parent / "boxteam-home"
    log_dir = workspace_root / ".boxteam" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "navigation-scope-backend.stdout.log"
    stderr_path = log_dir / "navigation-scope-backend.stderr.log"
    environment = os.environ.copy()
    environment["WORKSPACE_ROOT"] = str(workspace_root.resolve())
    environment["BOXTEAM_HOME"] = str(boxteam_home.resolve())
    environment.pop("BOXTEAM_GATEWAY_ROOT", None)
    environment["PYTHONUNBUFFERED"] = "1"
    with ExitStack() as log_files:
        stdout_file = log_files.enter_context(open(stdout_path, "a", encoding="utf-8"))
        stderr_file = log_files.enter_context(open(stderr_path, "a", encoding="utf-8"))
        process = subprocess.Popen(
            [
                str(python_executable),
                "-m",
                "uvicorn",
                "tests.integration.gateway.navigation_scope_proxy_backend:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=project_root,
            env=environment,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        handle = _NavigationScopeBackendProcess(
            process=process,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            port=port,
        )
        log_files.pop_all()
    try:
        wait_for_http_ok(f"http://127.0.0.1:{port}/api/v1/health", process)
    except BaseException as startup_error:
        startup_error.add_note(
            f"导航 scope 下游 FastAPI 启动日志: stdout={stdout_path}, stderr={stderr_path}"
        )
        try:
            _close_navigation_scope_backend(handle)
        except BaseException as cleanup_error:  # noqa: BLE001 - 同时保留启动与清理错误。
            raise BaseExceptionGroup(
                "导航 scope 测试后端启动失败且清理失败", [startup_error, cleanup_error],
            ) from None
        raise
    return handle


@pytest.fixture
def navigation_scope_proxy_runtime(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Iterator[_NavigationScopeProxyRuntime]:
    """启动隔离的下游 FastAPI/SQLite 服务和真实 Gateway 子进程。"""
    del integration_workspace_config_path
    workspace_root = Path(integration_workspace_root_path).resolve()
    boxteam_home = workspace_root.parent / "boxteam-home"
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    reset_gateway_persistent_state(workspace_root=workspace_root)

    gateway_root = boxteam_home / "state" / "gateway"
    peer_credential = FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    ).issue(
        connection_id="test-peer-connection",
        peer_gateway_id="gateway_peer_verified",
    )

    port_block = integration_port_block_for_file(Path(request.node.fspath))
    backend: _NavigationScopeBackendProcess | None = None
    gateway: GatewayProcess | None = None
    try:
        backend = _start_navigation_scope_backend(
            workspace_root=workspace_root,
            port=port_block.port(0),
        )
        gateway = start_gateway_process(
            workspace_root=workspace_root,
            default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=port_block.port(1),
            refresh_config=True,
        )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            response = client.get("/api/gateway/workspaces")
        assert response.status_code == 200, response.text
        workspace_id = read_workspace_id(workspace_root)
        workspace_entry, = response.json()["data"]["items"]
        assert workspace_entry["workspace_id"] == workspace_id
        assert workspace_entry["gateway_id"] == load_proxy_gateway_id()
        yield _NavigationScopeProxyRuntime(
            workspace_id=workspace_id,
            backend_port=backend.port,
            gateway=gateway,
            peer_credential=peer_credential,
        )
    finally:
        if gateway is not None:
            try:
                close_gateway_process(gateway)
            finally:
                if backend is not None:
                    _close_navigation_scope_backend(backend)
        elif backend is not None:
            _close_navigation_scope_backend(backend)


def _enqueue_payload(
    name: str,
    *,
    operation_id: str = _OPERATION_ID,
) -> dict[str, object]:
    return {
        "intents": [
            {
                "client_operation_id": operation_id,
                "client_sequence": 1,
                "kind": "create_folder",
                "base_catalog_revision": 0,
                "name": name,
            }
        ]
    }


@pytest.mark.asyncio
async def test_gateway_proxy_binds_navigation_queue_to_verified_identity(
    navigation_scope_proxy_runtime: _NavigationScopeProxyRuntime,
) -> None:
    """覆盖本机及联邦主体、幂等、伪造头与请求上下文重置。"""
    runtime = navigation_scope_proxy_runtime
    gateway_id = load_proxy_gateway_id()
    headers = {**LOCAL_TOKEN_HEADERS, "X-BoxTeam-Workspace-Id": runtime.workspace_id,
        "X-BoxTeam-Gateway-Id": "gateway:forged", "X-BoxTeam-Navigation-Actor": "user:forged"}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{runtime.gateway.port}", headers=headers, timeout=30) as client:
        async def enqueue(name: str, **extra: str) -> httpx.Response:
            return await client.post("/api/v1/session-catalog/operations:enqueue",
                headers=extra, json=_enqueue_payload(name, operation_id=_OPERATION_ID))

        first = await enqueue("Local operation")
        repeat = await enqueue("Local operation")
        assert first.status_code == repeat.status_code == 202
        local_seq = first.json()["data"]["receipts"][0]["queue_seq"]
        assert repeat.json()["data"]["receipts"][0]["queue_seq"] == local_seq
        assert (await enqueue("Changed preimage")).status_code == 409
        peer = await enqueue("Peer operation", **{"X-Local-Token": "", "X-BoxTeam-Federation-Token": runtime.peer_credential.token})
        assert peer.status_code == 202
        assert peer.json()["data"]["receipts"][0]["queue_seq"] != local_seq
        status = await client.get("/api/v1/session-catalog/operations", params={"operation_id": _OPERATION_ID},
            headers={"X-Request-ID": "local-status"})
        assert status.status_code == 200
        assert status.json()["data"]["items"][0]["queue_seq"] == local_seq
        assert status.headers["X-Request-ID"] == status.json()["request_id"] == "local-status"
        context_payload = {"resource_identity": str(workspace_session_inventory_identity(runtime.workspace_id)),
            "uri": workspace_session_inventory_uri(runtime.workspace_id), "scope": "workspace"}
        public_read = await client.post("/api/v1/context/read", json=context_payload)
        public_search = await client.post("/api/v1/context/search", json={**context_payload, "query": "operation"})
        assert public_read.status_code == public_search.status_code == 200
        assert public_read.json()["actor"] is None
        assert public_search.json()["actor"] is None
        assert public_read.json()["gateway_id"] == gateway_id
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{runtime.backend_port}", timeout=30) as backend:
            observed = await backend.get("/__test__/navigation-scopes")
        assert observed.status_code == 200
        scopes = observed.json()
        assert [scope["actor"] for scope in scopes] == ["local", "local", "local", "federation:gateway_peer_verified", "local"]
        assert all(scope["gateway_id"] == gateway_id and scope["workspace_id"] == runtime.workspace_id for scope in scopes)
