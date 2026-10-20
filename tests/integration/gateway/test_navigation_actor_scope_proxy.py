"""经真实 Gateway 与独立 FastAPI 下游验证导航 enqueue/status 的认证 scope。"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import httpx
import pytest

from app.core.identifier import create_prefixed_id
from app.core.path_utils import build_session_catalog_components
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.control.user_access import USER_ACCESS_COOKIE_NAME
from app.gateway.credentials import FederationCredential, FederationCredentialStore
from app.gateway.proxy_upstream import load_proxy_gateway_id
from app.gateway.workspace_ids import build_managed_local_workspace_id
from app.services.business.session_context_resource import (
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import (
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
    gateway_workspace_id: str
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

    workspace_id = load_or_create_workspace_id(workspace_root)
    catalog = build_session_catalog_components(
        workspace_root / ".boxteam" / "sessions", workspace_id
    )
    try:
        catalog.resolver.initialize()
    finally:
        catalog.close()

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
        workspace_entries = response.json()["data"]["items"]
        gateway_workspace_id = next(
            item["workspace_id"]
            for item in workspace_entries
            if item["workspace_id"] == build_managed_local_workspace_id(str(workspace_root))
        )
        yield _NavigationScopeProxyRuntime(
            workspace_id=workspace_id,
            backend_port=backend.port,
            gateway=gateway,
            gateway_workspace_id=gateway_workspace_id,
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
    """覆盖用户隔离、guest 稳定 scope、peer 认证、伪造头与请求上下文重置。"""
    runtime = navigation_scope_proxy_runtime
    gateway_id = load_proxy_gateway_id()
    gateway_workspace_id = runtime.gateway_workspace_id
    gateway_client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.gateway.port}",
        headers=LOCAL_TOKEN_HEADERS,
        timeout=30,
    )

    try:
        async def create_user_access(user_id: str, display_name: str) -> str:
            created = await gateway_client.post(
                "/api/gateway/users",
                json={"display_name": display_name, "user_id": user_id},
            )
            assert created.status_code == 200, created.text
            access = await gateway_client.post(
                f"/api/gateway/users/{user_id}/access",
                json={"client_label": f"{display_name} test client"},
            )
            assert access.status_code == 200, access.text
            cookie = access.cookies.get(USER_ACCESS_COOKIE_NAME)
            assert cookie
            gateway_client.cookies.clear()
            return cookie

        await acquire_gateway_guest(gateway_client)
        guest_cookie_one = gateway_client.cookies.get(USER_ACCESS_COOKIE_NAME)
        assert guest_cookie_one
        gateway_client.cookies.clear()
        await acquire_gateway_guest(gateway_client)
        guest_cookie_two = gateway_client.cookies.get(USER_ACCESS_COOKIE_NAME)
        assert guest_cookie_two
        gateway_client.cookies.clear()
        alice_cookie = await create_user_access("alice", "Alice")
        bob_cookie = await create_user_access("bob", "Bob")
        user_guest_cookie = await create_user_access("guest", "保留的名字空间")

        async def enqueue_as(
            *,
            request_id: str,
            name: str,
            cookie: str | None = None,
            federation_token: str | None = None,
            operation_id: str = _OPERATION_ID,
        ) -> httpx.Response:
            headers = {
                "X-Request-ID": request_id,
                "X-BoxTeam-Workspace-Id": gateway_workspace_id,
                "X-BoxTeam-Gateway-Id": "gateway:forged-client-value",
                "X-BoxTeam-Navigation-Actor": "user:forged-client-value",
            }
            if federation_token is not None:
                headers["X-Local-Token"] = ""
                headers["X-BoxTeam-Federation-Token"] = federation_token
            else:
                headers["X-Local-Token"] = LOCAL_TOKEN_HEADERS["X-Local-Token"]
            if cookie is not None:
                headers["Cookie"] = f"{USER_ACCESS_COOKIE_NAME}={cookie}"
            return await gateway_client.post(
                "/api/v1/session-catalog/operations:enqueue",
                headers=headers,
                json=_enqueue_payload(name, operation_id=operation_id),
            )

        async def status_as(
            *,
            request_id: str,
            cookie: str,
            operation_ids: Sequence[str] = (_OPERATION_ID,),
        ) -> httpx.Response:
            return await gateway_client.get(
                "/api/v1/session-catalog/operations",
                headers={
                    "X-Request-ID": request_id,
                    "X-BoxTeam-Workspace-Id": gateway_workspace_id,
                    "X-BoxTeam-Gateway-Id": "gateway:forged-status-value",
                    "X-Local-Token": LOCAL_TOKEN_HEADERS["X-Local-Token"],
                    "X-BoxTeam-Navigation-Actor": "user:forged-status-value",
                    "Cookie": f"{USER_ACCESS_COOKIE_NAME}={cookie}",
                },
                params=[("operation_id", operation_id) for operation_id in operation_ids],
            )

        alice_first = await enqueue_as(
            request_id="req-alice-first",
            name="Alice operation",
            cookie=alice_cookie,
        )
        assert alice_first.status_code == 202
        alice_repeat = await enqueue_as(
            request_id="req-alice-repeat",
            name="Alice operation",
            cookie=alice_cookie,
        )
        assert alice_repeat.status_code == 202
        assert (
            alice_repeat.json()["data"]["receipts"][0]["queue_seq"]
            == alice_first.json()["data"]["receipts"][0]["queue_seq"]
        )

        alice_conflict = await enqueue_as(
            request_id="req-alice-conflict",
            name="Alice changed preimage",
            cookie=alice_cookie,
        )
        assert alice_conflict.status_code == 409
        alice_private = await enqueue_as(
            request_id="req-alice-private",
            name="Alice private operation",
            cookie=alice_cookie,
            operation_id=_ALICE_PRIVATE_OPERATION_ID,
        )
        assert alice_private.status_code == 202

        bob_record = await enqueue_as(
            request_id="req-bob",
            name="Bob operation",
            cookie=bob_cookie,
        )
        guest_record = await enqueue_as(
            request_id="req-guest",
            name="Guest operation",
            cookie=guest_cookie_one,
        )
        guest_repeat_lease = await enqueue_as(
            request_id="req-guest-new-lease",
            name="Guest operation",
            cookie=guest_cookie_two,
        )
        user_named_guest = await enqueue_as(
            request_id="req-user-guest",
            name="User named guest operation",
            cookie=user_guest_cookie,
        )
        federation_record = await enqueue_as(
            request_id="req-federation-peer",
            name="Federation peer operation",
            federation_token=runtime.peer_credential.token,
        )
        assert bob_record.status_code == 202
        assert guest_record.status_code == 202
        assert guest_repeat_lease.status_code == 202
        assert user_named_guest.status_code == 202
        assert federation_record.status_code == 202
        assert (
            guest_repeat_lease.json()["data"]["receipts"][0]["queue_seq"]
            == guest_record.json()["data"]["receipts"][0]["queue_seq"]
        )

        alice_status = await status_as(
            request_id="req-alice-status",
            cookie=alice_cookie,
        )
        guest_status = await status_as(
            request_id="req-guest-status",
            cookie=guest_cookie_one,
        )
        alice_private_status = await status_as(
            request_id="req-alice-private-status",
            cookie=alice_cookie,
            operation_ids=(_ALICE_PRIVATE_OPERATION_ID,),
        )
        guest_private_status = await status_as(
            request_id="req-guest-private-status",
            cookie=guest_cookie_one,
            operation_ids=(_ALICE_PRIVATE_OPERATION_ID,),
        )
        assert alice_status.status_code == guest_status.status_code == 200
        assert alice_status.json()["data"]["items"][0]["operation_id"] == _OPERATION_ID
        assert guest_status.json()["data"]["items"][0]["operation_id"] == _OPERATION_ID
        assert (
            alice_status.json()["data"]["items"][0]["queue_seq"]
            != guest_status.json()["data"]["items"][0]["queue_seq"]
        )
        assert alice_private_status.json()["data"]["items"][0]["operation_id"] == (
            _ALICE_PRIVATE_OPERATION_ID
        )
        assert guest_private_status.json()["data"]["unknown_operation_ids"] == [
            _ALICE_PRIVATE_OPERATION_ID
        ]

        # 重复 query key 经真实 Gateway 保留全部 ID，认证 scope 仍隔离私有回执。
        alice_batch = await status_as(
            request_id="req-alice-batch-status",
            cookie=alice_cookie,
            operation_ids=(_OPERATION_ID, _ALICE_PRIVATE_OPERATION_ID),
        )
        guest_batch = await status_as(
            request_id="req-guest-batch-status",
            cookie=guest_cookie_one,
            operation_ids=(_OPERATION_ID, _ALICE_PRIVATE_OPERATION_ID),
        )
        assert alice_batch.status_code == guest_batch.status_code == 200
        alice_batch_data = alice_batch.json()["data"]
        guest_batch_data = guest_batch.json()["data"]
        assert {item["operation_id"] for item in alice_batch_data["items"]} == {
            _OPERATION_ID,
            _ALICE_PRIVATE_OPERATION_ID,
        }
        assert alice_batch_data["unknown_operation_ids"] == []
        assert [item["operation_id"] for item in guest_batch_data["items"]] == [_OPERATION_ID]
        assert guest_batch_data["unknown_operation_ids"] == [_ALICE_PRIVATE_OPERATION_ID]
        for response, request_id in (
            (alice_batch, "req-alice-batch-status"),
            (guest_batch, "req-guest-batch-status"),
        ):
            assert response.headers["X-Request-ID"] == request_id
            assert response.json()["request_id"] == request_id

        context_payload = {
            "resource_identity": str(workspace_session_inventory_identity(runtime.workspace_id)),
            "uri": workspace_session_inventory_uri(runtime.workspace_id),
            "scope": "workspace",
        }
        public_read = await gateway_client.post(
            "/api/v1/context/read",
            headers={
                "X-BoxTeam-Workspace-Id": gateway_workspace_id,
                "X-BoxTeam-Gateway-Id": "gateway:forged-public-value",
                "X-BoxTeam-Navigation-Actor": "user:forged-public-value",
                "Cookie": f"{USER_ACCESS_COOKIE_NAME}={guest_cookie_one}",
            },
            json=context_payload,
        )
        public_search = await gateway_client.post(
            "/api/v1/context/search",
            headers={
                "X-BoxTeam-Workspace-Id": gateway_workspace_id,
                "X-BoxTeam-Gateway-Id": "gateway:forged-public-value",
                "X-BoxTeam-Navigation-Actor": "user:forged-public-value",
                "Cookie": f"{USER_ACCESS_COOKIE_NAME}={guest_cookie_one}",
            },
            json={**context_payload, "query": "operation"},
        )
        assert public_read.status_code == public_search.status_code == 200
        assert public_read.json()["actor"] is None
        assert public_search.json()["actor"] is None
        assert public_read.json()["gateway_id"] == gateway_id

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{runtime.backend_port}",
            timeout=30,
        ) as backend_client:
            scopes_response = await backend_client.get(
                "/__test__/navigation-scopes"
            )
        assert scopes_response.status_code == 200, scopes_response.text
        scopes = scopes_response.json()
        expected_actors = [
            "user:alice",
            "user:alice",
            "user:alice",
            "user:alice",
            "user:bob",
            "guest",
            "guest",
            "user:guest",
            "federation:gateway_peer_verified",
            "user:alice",
            "guest",
            "user:alice",
            "guest",
            "user:alice",
            "guest",
        ]
        assert [scope["actor"] for scope in scopes] == expected_actors
        assert all(scope["gateway_id"] == gateway_id for scope in scopes)
        assert all(scope["workspace_id"] == runtime.workspace_id for scope in scopes)
        assert runtime.workspace_id != gateway_workspace_id
    finally:
        await gateway_client.aclose()
