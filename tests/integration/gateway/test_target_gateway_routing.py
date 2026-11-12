"""真实双 Gateway／Backend 验证目标归属；仅 SSH 执行与隧道使用本机替身。"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import patch

import commentjson
import httpx
import pytest
from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.workspace_identity import (
    load_or_create_workspace_id,
    workspace_identity_path,
)
from app.gateway import federation, remote_gateway
from app.gateway.control.gateway_registry.snapshot import read_workspace_registry
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.lifespan import lifespan
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.gateway.runtime.workspace import WorkspaceRuntime
from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


class HttpObservation(TypedDict):
    method: str
    path: str
    request_id: str | None
    sender_gateway_id: str | None
    target_gateway_id: str | None
    workspace_id: str | None
    status: int
    response_request_id: str | None
    server_port: int | None


class ObservedHttp:
    """仅旁路记录真实 socket 入站头与响应；不改请求、响应或生产路由。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.path = Path(os.environ["BOXTEAM_TEST_TARGET_HTTP_LOG"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def observe(message: Message) -> None:
            if scope["type"] == "http" and message["type"] == "http.response.start":
                incoming = Headers(scope=scope)
                outgoing = Headers(raw=message["headers"])
                server = scope.get("server")
                observation: HttpObservation = {
                    "method": scope["method"], "path": scope["path"],
                    "request_id": incoming.get("X-Request-ID"),
                    "sender_gateway_id": incoming.get("X-BoxTeam-Gateway-Id"),
                    "target_gateway_id": incoming.get(TARGET_GATEWAY_ID_HEADER),
                    "workspace_id": incoming.get("X-BoxTeam-Workspace-Id"),
                    "status": message["status"],
                    "response_request_id": outgoing.get("X-Request-ID"),
                    "server_port": server[1] if server is not None else None,
                }
                with self.path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(observation, ensure_ascii=False) + "\n")
            await send(message)

        await self.app(scope, receive, observe)


def _loopback_pairing_command(*, remote_command: str, **_connection: object) -> list[str]:
    """保留原配对凭据 CLI 与解析，仅把 SSH 的远程执行换成隔离 HOME 本机执行。"""
    words = shlex.split(remote_command)
    connection_id = words[words.index("--connection-id") + 1]
    peer_gateway_id = words[words.index("--peer-gateway-id") + 1]
    set_home_and_exec = (
        "import os, sys\n"
        "os.environ['BOXTEAM_HOME'] = sys.argv[1]\n"
        "os.environ.pop('BOXTEAM_GATEWAY_ROOT', None)\n"
        "os.execv(sys.argv[2], sys.argv[2:])\n"
    )
    return [
        sys.executable, "-c", set_home_and_exec, os.environ["BOXTEAM_TEST_TARGET_REMOTE_HOME"],
        sys.executable, "-m", "app.gateway.federation_pairing",
        "--connection-id", connection_id, "--peer-gateway-id", peer_gateway_id, "--json",
    ]


async def _loopback_gateway_tunnel(
    *, connection: RemoteGatewayConnection, **_options: object,
) -> WorkspaceRuntime:
    return WorkspaceRuntime(service_urls={
        "workspace_api": f"http://127.0.0.1:{connection.remote_gateway_port}",
    })


@asynccontextmanager
async def _loopback_lifespan(application: FastAPI) -> AsyncIterator[dict[str, Callable[[], Awaitable[None]] | None]]:
    with patch.object(federation, "build_ssh_command", _loopback_pairing_command), patch.object(
        remote_gateway, "start_remote_gateway_tunnel", _loopback_gateway_tunnel,
    ):
        async with lifespan(application) as state:
            yield state


def create_gateway_app() -> ASGIApp:
    from app.gateway.main import app

    advertised_version = os.environ.get("BOXTEAM_TEST_TARGET_FEDERATION_PROTOCOL_VERSION")
    if advertised_version is not None:
        from app.gateway.routes import federation as federation_routes

        # 仅改变目标进程正式发现端点声明的版本；响应与 DTO 仍由原生产路由生成。
        federation_routes.FEDERATION_PROTOCOL_VERSION = int(advertised_version)
    if "BOXTEAM_TEST_TARGET_REMOTE_HOME" in os.environ:
        app.router.lifespan_context = _loopback_lifespan
    return ObservedHttp(app)


def create_backend_app() -> ASGIApp:
    from app.main import app

    return ObservedHttp(app)


@dataclass(frozen=True)
class TargetGatewayNetwork:
    context: TestRunContext
    clients: tuple[httpx.Client, httpx.Client]
    workspace_id: str
    gateway_ids: tuple[str, str]
    native_routes: tuple[str, str]
    observation_paths: dict[str, Path]
    workspace_roots: tuple[Path, Path]
    gateway_config_paths: tuple[Path, Path]
    gateway_homes: tuple[Path, Path]
    stop_gateway: Callable[[int], None]
    restart_gateway: Callable[[int, int | None, dict[str, str]], httpx.Client]
    handoff_gateway: Callable[[dict[str, str]], httpx.Client]
    finish_gateway_handoff: Callable[[], None]
    gateway_history: list[dict[str, object]]


@pytest.fixture
def target_gateway_port(request: pytest.FixtureRequest) -> Callable[[int], int]:
    return integration_port_block_for_file(Path(request.node.path)).port


@pytest.fixture
def target_gateway_network(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    target_gateway_port: Callable[[int], int],
) -> Iterator[TargetGatewayNetwork]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace_a = Path(integration_workspace_root_path)
    assert workspace_a == context.workspace_root
    workspace_b = prepare_default_test_workspace(
        workspace_root=context.output_root / "peer/workspace",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    install_test_workspace_config(
        workspace_root=workspace_b, config_path=workspace_a / ".boxteam/workspace.jsonc",
        schema_path=workspace_a / ".boxteam/workspace_schema.jsonc",
    )
    workspace_id = load_or_create_workspace_id(workspace_a)
    # 显式克隆 UUID，两个原生 Gateway 各有独立目录与 catalog，不共享运行进程。
    shutil.copyfile(workspace_identity_path(workspace_a), workspace_identity_path(workspace_b))
    runtime = context.runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True, exist_ok=True)
    homes = (runtime / "gateway-a-home", runtime / "gateway-b-home")
    observations = {role: context.artifacts_dir / role / "http.jsonl" for role in (
        "gateway-a", "gateway-b", "backend-a", "backend-b",
    )}
    for path in observations.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    private_key = runtime / "pairing-key"
    shutil.copyfile(Path.cwd() / "asset/gateway_ssh/boxteam_gateway_e2e_ed25519", private_key)
    private_key.chmod(0o600)
    with ExitStack() as cleanup:
        lease = E2EProcessLease(runtime / "ports.json", tuple(target_gateway_port(offset) for offset in (0, 1, 20, 21, 22, 2)))
        lease.acquire()
        cleanup.callback(lease.release)
        backends = []
        for index, (workspace, offset, home) in enumerate(zip((workspace_a, workspace_b), (0, 20), homes, strict=True)):
            role = "backend-a" if index == 0 else "backend-b"
            backend = start_backend_process(
                workspace_root=str(workspace), port=target_gateway_port(offset), log_name=role,
                boxteam_home=home, artifacts_dir=observations[role].parent,
                asgi_app=f"{__name__}:create_backend_app", env_unset=("BOXTEAM_GATEWAY_ROOT",),
                env_overrides={
                    "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{target_gateway_port(offset + 1)}",
                    "BOXTEAM_TEST_TARGET_HTTP_LOG": str(observations[role]),
                },
            )
            cleanup.callback(close_backend_process, backend)
            assert backend.workspace_id == workspace_id
            backends.append(backend)
        clients: dict[int, httpx.Client] = {}
        gateway_ids: dict[int, str] = {}
        routes: dict[int, str] = {}
        gateways: list[GatewayProcess | None] = [None, None]
        previous_gateway: GatewayProcess | None = None
        gateway_ports = [target_gateway_port(1), target_gateway_port(21)]
        stderr_offsets: dict[int, int] = {}
        gateway_history: list[dict[str, object]] = []

        def close_gateway(index: int, gateway: GatewayProcess) -> None:
            nonlocal previous_gateway
            close_gateway_process(gateway)
            role = "gateway-b" if index else "gateway-a"
            with (observations[role].parent / "gateway.stderr.log").open("rb") as output:
                output.seek(stderr_offsets.pop(gateway.process.pid))
                process_log = output.read().decode("utf-8", errors="replace")
            # 同 PID 保留完整 traceback，分别核对监听排空与 ASGI 清理阶段。
            records = [record for record in re.split(r"(?m)(?=^\d{4}-\d{2}-\d{2}T)", process_log)
                       if f"pid={gateway.process.pid} " in record.partition("\n")[0]]
            draining_start = next((index for index, record in enumerate(records) if " Shutting down" in record.partition("\n")[0]), len(records))
            shutdown_start = next((index for index, record in enumerate(records) if " Waiting for application shutdown." in record.partition("\n")[0]), len(records))
            shutdown_log = "\n".join(records[shutdown_start:])
            draining_errors = [record for record in records[draining_start:shutdown_start] if "[ERROR]" in record.partition("\n")[0] or record.startswith("ERROR:")]
            # B 已迁端口时，旧 A 在监听排空期间仍可能重拨原 B；其它错误不能略过。
            expected_draining_errors = [record for record in draining_errors
                                       if index == 0 and gateway_ports[1] == target_gateway_port(22)
                                       and "logger=app.gateway.federation.channel_owner 联邦 spoke channel 无法连接:" in record.partition("\n")[0]
                                       and re.fullmatch(rf"ConnectionRefusedError: .*Connect call failed \('127\.0\.0\.1', {target_gateway_port(21)}\)", record.rstrip().splitlines()[-1])]
            gateway_history.append({
                "action": "stopped", "index": index, "port": gateway.port,
                "pid": gateway.process.pid, "returncode": gateway.process.returncode,
                "lifespan_cleanup_complete": "Gateway lifespan 清理完成" in shutdown_log,
                "application_shutdown_complete": "Application shutdown complete." in shutdown_log,
                "listener_draining_errors": draining_errors,
                "expected_offline_draining_errors": expected_draining_errors,
                "unexpected_draining_errors": [record for record in draining_errors if record not in expected_draining_errors],
                "shutdown_errors": [line for line in shutdown_log.splitlines() if "[ERROR]" in line or line.startswith("ERROR:")],
            })
            if gateways[index] is gateway:
                gateways[index] = None
            if previous_gateway is gateway:
                previous_gateway = None

        def stop_gateway(index: int) -> None:
            gateway = gateways[index]
            if gateway is not None:
                close_gateway(index, gateway)

        def finish_gateway_handoff() -> None:
            assert previous_gateway is not None
            close_gateway(0, previous_gateway)

        def close_gateways(index: int) -> None:
            if index == 0 and previous_gateway is not None:
                finish_gateway_handoff()
            stop_gateway(index)

        for index in (1, 0):
            cleanup.callback(close_gateways, index)

        def start_gateway(
            index: int, port: int | None, extra_env: dict[str, str],
        ) -> httpx.Client:
            if port is not None:
                assert port in lease.ports
                gateway_ports[index] = port
            role = "gateway-b" if index else "gateway-a"
            stderr_path = observations[role].parent / "gateway.stderr.log"
            stderr_offset = stderr_path.stat().st_size if stderr_path.exists() else 0
            environment = {"BOXTEAM_TEST_TARGET_HTTP_LOG": str(observations[role])}
            if index == 0:
                environment["BOXTEAM_TEST_TARGET_REMOTE_HOME"] = str(homes[1])
            environment.update(extra_env)
            gateway_history.append({
                "action": "starting", "index": index, "port": gateway_ports[index],
                "advertised_protocol_version": int(environment.get("BOXTEAM_TEST_TARGET_FEDERATION_PROTOCOL_VERSION", "2")),
                "candidate_startup": "BOXTEAM_CONFIG_CANDIDATE_REF" in environment,
            })
            gateway = start_gateway_process(
                workspace_root=(workspace_a, workspace_b)[index],
                default_backend_url=f"http://127.0.0.1:{backends[index].port}", port=gateway_ports[index],
                boxteam_home=homes[index], artifacts_dir=observations[role].parent,
                asgi_app=f"{__name__}:create_gateway_app", extra_env=environment,
            )
            gateways[index] = gateway
            stderr_offsets[gateway.process.pid] = stderr_offset
            gateway_history.append({
                "action": "started", "index": index, "port": gateway.port,
                "pid": gateway.process.pid,
                "advertised_protocol_version": int(environment.get("BOXTEAM_TEST_TARGET_FEDERATION_PROTOCOL_VERSION", "2")),
                "candidate_startup": "BOXTEAM_CONFIG_CANDIDATE_REF" in environment,
            })
            client = cleanup.enter_context(httpx.Client(
                base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
                timeout=25, trust_env=False,
            ))
            health = client.get("/api/gateway/health")
            assert health.status_code == 200, health.text
            gateway_id = health.json()["data"]["gateway_id"]
            if index in gateway_ids:
                assert gateway_id == gateway_ids[index]
            gateway_ids[index] = gateway_id
            return client

        def restart_gateway(
            index: int, port: int | None, extra_env: dict[str, str],
        ) -> httpx.Client:
            stop_gateway(index)
            return start_gateway(index, port, extra_env)

        def handoff_gateway(extra_env: dict[str, str]) -> httpx.Client:
            nonlocal previous_gateway
            assert previous_gateway is None and gateways[0] is not None
            previous_gateway = gateways[0]
            candidate_port = target_gateway_port(2) if previous_gateway.port == target_gateway_port(1) else target_gateway_port(1)
            return start_gateway(0, candidate_port, extra_env)

        # 先启动远端，A 的产品注册随后通过真实 HTTP 发现它。
        for index in (1, 0):
            client = restart_gateway(index, None, {})
            registered = client.get("/api/gateway/workspaces")
            assert registered.status_code == 200, registered.text
            native, = registered.json()["data"]["items"]
            assert native["workspace_id"] == workspace_id
            assert native["gateway_id"] == gateway_ids[index]
            assert "backend_workspace_id" not in native
            routes[index] = native["workspace_id"]
            clients[index] = client
        assert gateway_ids[0] != gateway_ids[1]
        assert backends[0].process.pid != backends[1].process.pid
        (context.artifacts_dir / "topology.json").write_text(json.dumps({
            "workspace_id": workspace_id, "gateway_ids": gateway_ids, "native_routes": routes,
            "ports": lease.ports, "backend_pids": [backend.process.pid for backend in backends],
            "private_key": str(private_key), "transport_boundary": "SSH 执行改为原 pairing CLI 本机执行，隧道改为 loopback URL",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        yield TargetGatewayNetwork(
            context, (clients[0], clients[1]), workspace_id,
            (gateway_ids[0], gateway_ids[1]), (routes[0], routes[1]), observations,
            (workspace_a, workspace_b),
            (homes[0] / "config/gateway.jsonc", homes[1] / "config/gateway.jsonc"),
            homes, stop_gateway, restart_gateway, handoff_gateway,
            finish_gateway_handoff, gateway_history,
        )


def _observations(path: Path, request_id: str) -> list[HttpObservation]:
    return [record for line in path.read_text(encoding="utf-8").splitlines()
            if (record := cast(HttpObservation, json.loads(line)))["request_id"] == request_id]


def test_real_gateways_keep_same_uuid_owners_and_target_carrier_distinct(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    local = network.clients[0]
    gateway_a, gateway_b = network.gateway_ids
    native_a, native_b = network.native_routes
    evidence: list[dict[str, object]] = []

    def request(
        client: httpx.Client, method: str, path: str, request_id: str, *,
        headers: dict[str, str] | None = None, json: dict[str, object] | None = None,
    ) -> httpx.Response:
        response = client.request(method, path, headers={**(headers or {}), "X-Request-ID": request_id}, json=json)
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        evidence.append({"request_id": request_id, "path": path, "status": response.status_code, "body": response.json()})
        return response

    try:
        session_ids = []
        for index, client in enumerate(network.clients):
            created = request(client, "POST", "/api/v1/sessions", f"req_target_seed_{index}", json={"title": f"原生 Gateway {index}"})
            assert created.status_code == 200, created.text
            session_ids.append(created.json()["data"]["session_id"])
        assert session_ids[0] != session_ids[1]
        topology = json.loads((network.context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
        registered = request(local, "POST", "/api/gateway/remote-gateways", "req_target_register", json={
            "name": "真实 Gateway B", "host": "127.0.0.1", "port": 22, "username": "integration",
            "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
        })
        assert registered.status_code == 200, registered.text
        items = registered.json()["data"]["items"]
        projected, = [item for item in items if item["connection_kind"] == "remote_gateway"]
        projection = projected["workspace_id"]
        assert projection == native_a == native_b == network.workspace_id
        assert projected["gateway_id"] == gateway_b
        assert "backend_workspace_id" not in projected
        discovery = request(local, "GET", "/api/gateway/search/workspaces", "req_target_discovery")
        assert discovery.status_code == 200, discovery.text
        owners = {(item["owner"]["gateway_id"], item["owner"]["workspace_id"]) for item in discovery.json()["data"]["items"]}
        assert owners == {
            (gateway_a, network.workspace_id),
            (gateway_b, network.workspace_id),
        }
        activated = request(local, "POST", f"/api/gateway/workspaces/{projection}/activate", "req_target_activate_remote",
                            headers={TARGET_GATEWAY_ID_HEADER: gateway_b})
        assert activated.status_code == 200, activated.text
        canonical = f"/api/v1/workspaces/{network.workspace_id}/sessions"
        successes = [
            ("req_target_native_a", canonical, {TARGET_GATEWAY_ID_HEADER: gateway_a}, 0),
            ("req_target_native_b", canonical, {
                TARGET_GATEWAY_ID_HEADER: gateway_b, "X-BoxTeam-Workspace-Id": native_a,
                "X-BoxTeam-Gateway-Id": "gateway_" + "f" * 32,
            }, 1),
            ("req_target_native_uuid", "/api/v1/sessions", {"X-BoxTeam-Workspace-Id": network.workspace_id}, 0),
            ("req_target_active_remote", "/api/v1/sessions", {}, 1),
        ]
        for request_id, path, headers, index in successes:
            response = request(local, "GET", path, request_id, headers=headers)
            assert response.status_code == 200, response.text
            assert [item["session_id"] for item in response.json()["data"]["items"]] == [session_ids[index]]
            backend_role = "backend-b" if index else "backend-a"
            downstream, = _observations(network.observation_paths[backend_role], request_id)
            assert downstream["sender_gateway_id"] == (gateway_b if index else gateway_a)
            assert downstream["target_gateway_id"] is None
            assert downstream["workspace_id"] == network.workspace_id
            assert downstream["response_request_id"] == request_id
            assert _observations(network.observation_paths["backend-a" if index else "backend-b"], request_id) == []
            remote_hop = _observations(network.observation_paths["gateway-b"], request_id)
            if index:
                forwarded, = remote_hop
                assert forwarded["target_gateway_id"] == gateway_b
                assert forwarded["sender_gateway_id"] == gateway_a
                assert forwarded["workspace_id"] == native_b
                assert forwarded["response_request_id"] == request_id
            else:
                assert remote_hop == []
        failures = [
            ("req_target_unknown", canonical, {TARGET_GATEWAY_ID_HEADER: "gateway_" + "e" * 32}, 404),
            ("req_target_mismatch", canonical, {
                TARGET_GATEWAY_ID_HEADER: gateway_b,
                "X-BoxTeam-Workspace-Id": "00000000-0000-4000-8000-000000000099",
            }, 400),
            ("req_target_empty", canonical, {TARGET_GATEWAY_ID_HEADER: ""}, 400),
        ]
        for request_id, path, headers, status in failures:
            rejected = request(local, "GET", path, request_id, headers=headers)
            assert rejected.status_code == status, rejected.text
            for role in ("gateway-b", "backend-a", "backend-b"):
                assert _observations(network.observation_paths[role], request_id) == []

        first_connection = projected["remote_gateways"][0]["gateway_connection_id"]
        another = request(local, "POST", "/api/gateway/remote-gateways", "req_target_second_connection", json={
            "name": "Gateway B 的另一条连接", "host": "127.0.0.1", "port": 22,
            "username": "integration-alternate", "private_key_path": topology["private_key"],
            "remote_gateway_port": topology["ports"][3],
        })
        assert another.status_code == 200, another.text
        same_source, = [item for item in another.json()["data"]["items"]
                        if item["gateway_id"] == gateway_b]
        assert same_source["workspace_id"] == network.workspace_id
        connection_ids = {item["gateway_connection_id"] for item in same_source["remote_gateways"]}
        assert first_connection in connection_ids and len(connection_ids) == 2
        assert same_source["status"] == same_source["services"]["workspace_api"]["status"] == "offline"
        ambiguous = request(local, "GET", canonical, "req_target_ambiguous_connection",
                            headers={TARGET_GATEWAY_ID_HEADER: gateway_b})
        assert ambiguous.status_code == 500, ambiguous.text
        for role in ("gateway-b", "backend-a", "backend-b"):
            assert _observations(network.observation_paths[role], "req_target_ambiguous_connection") == []

        # 请求没有取得占用时，管理入口仍可解除歧义；资源 owner 与 UUID 不变。
        unbound = request(local, "DELETE", f"/api/gateway/remote-gateways/{first_connection}", "req_target_unbind")
        assert unbound.status_code == 200, unbound.text
        retained, = [item for item in unbound.json()["data"]["items"] if item["gateway_id"] == gateway_b]
        assert retained["workspace_id"] == network.workspace_id
        assert {item["gateway_connection_id"] for item in retained["remote_gateways"]} == connection_ids - {first_connection}
        assert retained["status"] == "ready"
        resumed = request(local, "GET", canonical, "req_target_after_unbind",
                          headers={TARGET_GATEWAY_ID_HEADER: gateway_b})
        assert resumed.status_code == 200, resumed.text
        assert [item["session_id"] for item in resumed.json()["data"]["items"]] == [session_ids[1]]
        forwarded, = _observations(network.observation_paths["gateway-b"], "req_target_after_unbind")
        backend, = _observations(network.observation_paths["backend-b"], "req_target_after_unbind")
        assert (forwarded["sender_gateway_id"], forwarded["target_gateway_id"], forwarded["workspace_id"]) == (gateway_a, gateway_b, network.workspace_id)
        assert (backend["sender_gateway_id"], backend["target_gateway_id"], backend["workspace_id"]) == (gateway_b, None, network.workspace_id)
        assert _observations(network.observation_paths["backend-a"], "req_target_after_unbind") == []
        final = request(local, "GET", "/api/gateway/workspaces", "req_target_final_state")
        assert final.status_code == 200, final.text
        assert final.json()["data"]["active_workspace"] == {"gateway_id": gateway_b, "workspace_id": network.workspace_id}
    finally:
        (network.context.artifacts_dir / "target-routing-http.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )


def test_real_gateways_authorize_source_edits_at_native_owner(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    local, native = network.clients
    gateway_a, gateway_b = network.gateway_ids
    source_key = "workspace_root_mutable_override"
    source_path = f"/api/v1/workspaces/{network.workspace_id}/config/sources/{source_key}"
    target_headers = {TARGET_GATEWAY_ID_HEADER: gateway_b}
    workspace_files = tuple(root / ".boxteam/workspace.jsonc" for root in network.workspace_roots)
    originals = tuple(path.read_bytes() for path in workspace_files)
    evidence: list[dict[str, object]] = []

    def exchange(
        client: httpx.Client, method: str, path: str, request_id: str, *,
        remote: bool = False, body: dict[str, object] | None = None,
    ) -> httpx.Response:
        response = client.request(method, path, json=body, headers={
            **(target_headers if remote else {}), "X-Request-ID": request_id,
        })
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        evidence.append({"request_id": request_id, "path": path, "status": response.status_code, "body": response.json()})
        return response

    def publish_policy(rules: list[dict[str, str]], label: str) -> None:
        # 本机用户编辑正式 JSONC，由真实 watcher 发布；不直接改内存策略或数据库。
        config_path = network.gateway_config_paths[1]
        before = exchange(native, "GET", "/api/gateway/config/reload-status", f"{label}-before").json()["data"]
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": rules}
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        deadline = time.monotonic() + 15
        for attempt in range(76):
            current = exchange(native, "GET", "/api/gateway/config/reload-status", f"{label}-poll-{attempt}").json()["data"]
            if current["revision"] != before["revision"] and current["healthy"] is True:
                assert current["state"] == "active" and current["restart_required"] is False
                return
            assert time.monotonic() < deadline, current
            time.sleep(0.2)
        raise AssertionError("配置 watcher 未在期限内发布策略")

    try:
        topology = json.loads((network.context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
        registered = exchange(local, "POST", "/api/gateway/remote-gateways", "config-register-peer", body={
            "name": "配置目标 B", "host": "127.0.0.1", "port": 22, "username": "config-integration",
            "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
        })
        assert registered.status_code == 200, registered.text
        projected, = [item for item in registered.json()["data"]["items"] if item["gateway_id"] == gateway_b]
        assert projected["workspace_id"] == network.workspace_id

        denied = exchange(local, "GET", source_path, "config-default-denied", remote=True)
        assert denied.status_code == 403, denied.text
        assert _observations(network.observation_paths["backend-b"], "config-default-denied") == []
        baseline = exchange(native, "GET", source_path, "config-native-baseline")
        assert baseline.status_code == 200, baseline.text
        document = baseline.json()["data"]
        assert (document["gateway_id"], document["workspace_id"], document["source_key"]) == (gateway_b, network.workspace_id, source_key)
        edited = commentjson.loads(document["jsonc"])
        edited.setdefault("ui", {})["ignored_paths"] = ["**/peer-only/**"]
        intent = {
            "gateway_id": gateway_b, "workspace_id": network.workspace_id, "source_key": source_key,
            "jsonc": json.dumps(edited, ensure_ascii=False, indent=2) + "\n",
            "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
            "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
            "expected_active_digest": document["active_digest"], "idempotency_key": "network-source-edit",
        }
        publish_policy([{"capability": "configuration_read", "effect": "allow"}], "config-wildcard")
        wildcard = exchange(local, "GET", source_path, "config-wildcard-denied", remote=True)
        assert wildcard.status_code == 403, wildcard.text
        principal = principal_ref_for_gateway(gateway_a)
        read_rule = {"capability": "configuration_read", "effect": "allow", "principal_ref": principal}
        publish_policy([read_rule], "config-read-only")
        readable = exchange(local, "GET", source_path, "config-read-allowed", remote=True)
        assert readable.status_code == 200, readable.text
        assert readable.json()["data"] == document
        for method, suffix in (("POST", "/preflight"), ("PUT", "")):
            rejected = exchange(local, method, source_path + suffix, f"config-read-only-{method}", remote=True, body=intent)
            assert rejected.status_code == 403, rejected.text
            assert _observations(network.observation_paths["backend-b"], f"config-read-only-{method}") == []
        assert tuple(path.read_bytes() for path in workspace_files) == originals

        publish_policy([read_rule, {"capability": "configuration_write", "effect": "allow", "principal_ref": principal}], "config-read-write")
        preflight = exchange(local, "POST", source_path + "/preflight", "config-preflight", remote=True, body=intent)
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["data"]["candidate"]["ui"]["ignored_paths"] == ["**/peer-only/**"]
        assert preflight.json()["data"]["changed_paths"] == ["/ui/ignored_paths"]
        assert tuple(path.read_bytes() for path in workspace_files) == originals
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending_saves = [executor.submit(
                exchange, local, "PUT", source_path, request_id, remote=True, body=intent,
            ) for request_id in ("config-save", "config-save-concurrent")]
            saved, concurrent_save = [pending.result() for pending in pending_saves]
        assert saved.status_code == concurrent_save.status_code == 200, (saved.text, concurrent_save.text)
        results = [response.json()["data"] for response in (saved, concurrent_save)]
        completed = [document for document in results if document["edit"]["state"] == "active"]
        assert completed, results
        result = completed[0]
        for document in results:
            assert document["edit"]["candidate_id"] == result["edit"]["candidate_id"]
            assert document["edit"]["idempotency_key"] == intent["idempotency_key"]
            assert document["edit"]["file_digest"] == result["edit"]["file_digest"]
        assert result["status"]["state"] == "active" and result["status"]["restart_required"] is False
        assert result["effective"]["ui"]["ignored_paths"] == ["**/peer-only/**"]
        assert workspace_files[0].read_bytes() == originals[0]
        assert commentjson.loads(workspace_files[1].read_text(encoding="utf-8"))["ui"]["ignored_paths"] == ["**/peer-only/**"]
        for request_id in ("config-read-allowed", "config-preflight", "config-save"):
            forwarded, = _observations(network.observation_paths["gateway-b"], request_id)
            backend, = _observations(network.observation_paths["backend-b"], request_id)
            assert (forwarded["sender_gateway_id"], forwarded["target_gateway_id"], forwarded["workspace_id"]) == (gateway_a, gateway_b, network.workspace_id)
            assert (backend["sender_gateway_id"], backend["target_gateway_id"], backend["workspace_id"]) == (gateway_b, None, network.workspace_id)
            assert _observations(network.observation_paths["backend-a"], request_id) == []
        retried = exchange(local, "PUT", source_path, "config-save-retry", remote=True, body=intent)
        assert retried.status_code == 200, retried.text
        assert retried.json()["data"]["edit"] == result["edit"]
        recovered = exchange(local, "GET", source_path + "?idempotency_key=network-source-edit", "config-query-original", remote=True)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"]["edit"] == result["edit"]
        stale = exchange(local, "PUT", source_path, "config-stale-baseline", remote=True, body={**intent, "idempotency_key": "network-stale-edit"})
        assert stale.status_code == 409, stale.text
        events = exchange(local, "GET", f"/api/v1/workspaces/{network.workspace_id}/config/events", "config-accepted-events", remote=True)
        assert events.status_code == 200, events.text
        committed = [event for event in events.json()["data"]["events"] if event["idempotency_key"] == intent["idempotency_key"]]
        assert len(committed) == 1 and committed[0]["result"] == "applied"
        assert committed[0]["candidate_id"] == result["edit"]["candidate_id"]

        publish_policy([], "config-revoke")
        revoked = exchange(local, "GET", source_path, "config-revoked-read", remote=True)
        assert revoked.status_code == 403, revoked.text
        assert _observations(network.observation_paths["backend-b"], "config-revoked-read") == []
        assert workspace_files[0].read_bytes() == originals[0]
        local_config = exchange(local, "GET", f"/api/v1/workspaces/{network.workspace_id}/config", "config-local-owner", body=None)
        assert local_config.status_code == 200, local_config.text
        assert local_config.json()["data"]["ignored_paths"] != ["**/peer-only/**"]
    finally:
        (network.context.artifacts_dir / "configuration-source-http.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )


def test_real_gateways_recover_original_owner_after_connection_update_and_offline_restart(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    local, native = network.clients
    gateway_a, gateway_b = network.gateway_ids
    owner = {"gateway_id": gateway_b, "workspace_id": network.workspace_id}
    canonical = f"/api/v1/workspaces/{network.workspace_id}/sessions"
    target_headers = {TARGET_GATEWAY_ID_HEADER: gateway_b}
    connection_id = "connection_identity_recovery"
    state_path = network.gateway_homes[0] / "state/gateway/gateway.sqlite"
    credential_store = FederationCredentialStore(
        storage_path=network.gateway_homes[0] / "state/gateway/credentials/federation.json",
    )
    topology = json.loads((network.context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    original_port, updated_port = topology["ports"][3:5]
    evidence: list[dict[str, object]] = []
    snapshots: list[dict[str, object]] = []

    def exchange(
        client: httpx.Client, method: str, path: str, request_id: str, *,
        headers: dict[str, str] | None = None, body: dict[str, object] | None = None,
    ) -> httpx.Response:
        response = client.request(method, path, json=body, headers={
            **(headers or {}), "X-Request-ID": request_id,
        })
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        evidence.append({"request_id": request_id, "path": path, "status": response.status_code, "body": response.json()})
        return response

    def projection_snapshot(label: str) -> tuple[dict[str, object], dict[str, object]]:
        # 只读原 writer 的完整快照；不打开第二个 owner、不构造或补写恢复事实。
        with closing(sqlite3.connect(f"{state_path.as_uri()}?mode=ro", uri=True)) as database:
            database.execute("BEGIN")
            snapshot = read_workspace_registry(database)
        assert snapshot is not None
        targets = cast(list[dict[str, object]], snapshot["targets"])
        target, = [item for item in targets if (item["gateway_id"], item["workspace_id"]) == (gateway_b, network.workspace_id)]
        connections = cast(list[dict[str, object]], snapshot["remote_gateway_connections"])
        connection, = connections
        assert target["owner"] == "remote_projection"
        assert target["remote_gateway_connection_ids"] == [connection_id]
        assert target["target_generation"] and target["runtime_lease_id"]
        assert connection["connection_id"] == connection_id
        assert connection["remote_gateway_id"] == gateway_b
        assert connection["source_owner"] == "config"
        assert connection["protocol_version"] == 2
        assert "backend_workspace_id" not in target and "remote_workspace_id" not in connection
        snapshots.append({"label": label, "registry": snapshot})
        return target, connection

    def publish_connection(port: int, label: str) -> httpx.Client:
        nonlocal local
        before = exchange(local, "GET", "/api/gateway/config/reload-status", f"{label}-before").json()["data"]
        config_path = network.gateway_config_paths[0]
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config["workspaces"] = [{
            "kind": "remote_gateway", "connection_id": connection_id, "name": "恢复目标 B",
            "host": "127.0.0.1", "port": 22, "username": "identity-recovery",
            "private_key_path": topology["private_key"], "remote_gateway_port": port,
        }]
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        deadline = time.monotonic() + 60
        while True:
            status = exchange(local, "GET", "/api/gateway/config/reload-status", f"{label}-pending-{len(evidence)}").json()["data"]
            if status["state"] == "pending_restart" and status["candidate_ref"]:
                break
            assert time.monotonic() < deadline, status
            time.sleep(0.25)
        assert status["restart_required"] is True
        assert status["active_revision"] == before["active_revision"]
        assert "/workspaces" in status["deferred_paths"] or any(path.startswith("/workspaces/") for path in status["deferred_paths"])
        with closing(sqlite3.connect(f"{state_path.as_uri()}?mode=ro", uri=True)) as database:
            database.execute("BEGIN")
            intent = database.execute(
                "SELECT gateway_id, old_generation, target_generation, fencing_token, state, candidate_id "
                "FROM gateway_restart_intent WHERE candidate_ref = ?",
                (status["candidate_ref"],),
            ).fetchone()
            assert intent is not None and intent[0] == gateway_a and intent[1] is not None
            assert intent[4] == "pending"
            old_generation_id, new_generation_id = intent[1:3]
            pending = database.execute(
                "SELECT pending_revision FROM config_pending_candidate WHERE config_domain = 'gateway' AND candidate_id = ?",
                (intent[5],),
            ).fetchone()
            assert pending is not None and pending[0] == status["pending_revision"]
            old_serving = database.execute(
                "SELECT state, listener_state, process_id FROM gateway_runtime_generation WHERE generation_id = ?",
                (old_generation_id,),
            ).fetchone()
        assert old_serving is not None and old_serving[:2] == ("active", "serving")
        # 真实候选先在另一租约端口健康并完成 promotion，随后才停原 PID。
        local = network.handoff_gateway({
            "BOXTEAM_CONFIG_CANDIDATE_REF": status["candidate_ref"],
            "BOXTEAM_CONFIG_GENERATION": new_generation_id,
            "BOXTEAM_CONFIG_FENCING_TOKEN": intent[3],
        })
        active = exchange(local, "GET", "/api/gateway/config/reload-status", f"{label}-active").json()["data"]
        assert active["state"] == "active" and active["restart_required"] is False
        assert active["candidate_ref"] is None and active["pending_revision"] is None
        assert active["active_revision"] > before["active_revision"]
        with closing(sqlite3.connect(f"{state_path.as_uri()}?mode=ro", uri=True)) as database:
            new_generation = database.execute(
                "SELECT state, listener_state, loaded_source, process_id FROM gateway_runtime_generation WHERE generation_id = ?",
                (new_generation_id,),
            ).fetchone()
            old_generation_row = database.execute(
                "SELECT state, listener_state, process_id FROM gateway_runtime_generation WHERE generation_id = ?",
                (old_generation_id,),
            ).fetchone()
        assert new_generation is not None and new_generation[:3] == ("active", "serving", "pending")
        assert old_generation_row is not None and old_generation_row[:2] == ("active", "draining")
        assert old_generation_row[2] == old_serving[2] != new_generation[3]
        assert not any(event["action"] == "stopped" and event["pid"] == old_serving[2] for event in network.gateway_history)
        network.finish_gateway_handoff()
        with closing(sqlite3.connect(f"{state_path.as_uri()}?mode=ro", uri=True)) as database:
            closed = database.execute(
                "SELECT state, listener_state, process_id FROM gateway_runtime_generation WHERE generation_id = ?",
                (old_generation_id,),
            ).fetchone()
        assert closed == ("closed", "closed", old_serving[2])
        stopped, = [event for event in network.gateway_history if event["action"] == "stopped" and event["pid"] == old_generation_row[2]]
        # Uvicorn 清理结束后重新抛出原 SIGTERM；POSIX 的合法退出码为 -SIGTERM。
        assert stopped["returncode"] in {0, -signal.SIGTERM}
        assert stopped["lifespan_cleanup_complete"] is stopped["application_shutdown_complete"] is True
        assert stopped["unexpected_draining_errors"] == []
        assert stopped["shutdown_errors"] == []
        evidence.append({
            "phase": label, "new_generation": new_generation, "old_serving": old_serving,
            "old_draining": old_generation_row, "old_closed": closed,
            "old_generation_id": old_generation_id, "new_generation_id": new_generation_id,
        })
        return local

    def assert_remote_sessions(request_id: str, session_id: str, *, active: bool = False) -> None:
        response = exchange(local, "GET", "/api/v1/sessions" if active else canonical, request_id,
                            headers={} if active else target_headers)
        assert response.status_code == 200, response.text
        assert [item["session_id"] for item in response.json()["data"]["items"]] == [session_id]
        forwarded, = _observations(network.observation_paths["gateway-b"], request_id)
        backend, = _observations(network.observation_paths["backend-b"], request_id)
        assert (forwarded["sender_gateway_id"], forwarded["target_gateway_id"], forwarded["workspace_id"]) == (gateway_a, gateway_b, network.workspace_id)
        assert forwarded["server_port"] == updated_port
        assert (backend["sender_gateway_id"], backend["target_gateway_id"], backend["workspace_id"]) == (gateway_b, None, network.workspace_id)
        assert forwarded["response_request_id"] == backend["response_request_id"] == request_id
        assert _observations(network.observation_paths["backend-a"], request_id) == []

    try:
        local_seed = exchange(local, "POST", "/api/v1/sessions", "recovery-local-seed", body={"title": "本机独有会话"})
        remote_seed = exchange(native, "POST", "/api/v1/sessions", "recovery-remote-seed", body={"title": "远端独有会话"})
        assert local_seed.status_code == remote_seed.status_code == 200
        remote_session_id = remote_seed.json()["data"]["session_id"]
        assert local_seed.json()["data"]["session_id"] != remote_session_id
        local = publish_connection(original_port, "recovery-register")
        activated = exchange(local, "POST", f"/api/gateway/workspaces/{network.workspace_id}/activate",
                             "recovery-activate", headers=target_headers)
        assert activated.status_code == 200 and activated.json()["data"]["active_workspace"] == owner
        before_target, before_connection = projection_snapshot("registered")
        old_credential = credential_store.get(connection_id)
        assert old_credential.connection_id == connection_id and old_credential.peer_gateway_id == gateway_a

        native = network.restart_gateway(1, updated_port, {})
        with (
            httpx.Client(base_url=f"http://127.0.0.1:{original_port}", trust_env=False, timeout=2) as old_listener,
            pytest.raises(httpx.ConnectError),
        ):
            old_listener.get("/api/gateway/health")
        local = publish_connection(updated_port, "recovery-update")
        updated_target, updated_connection = projection_snapshot("updated")
        assert updated_target["target_generation"] != before_target["target_generation"]
        assert updated_target["runtime_lease_id"] != before_target["runtime_lease_id"]
        assert before_connection["remote_gateway_port"] == original_port
        assert updated_connection["remote_gateway_port"] == updated_port
        current_credential = credential_store.get(connection_id)
        assert current_credential.peer_gateway_id == old_credential.peer_gateway_id
        assert current_credential.token != old_credential.token
        stale = exchange(native, "GET", "/api/gateway/federation/manifest", "recovery-stale-credential", headers={
            "X-BoxTeam-Federation-Token": old_credential.token,
        })
        assert stale.status_code == 401, stale.text
        assert "无效或已过期" in stale.json()["detail"]
        assert_remote_sessions("recovery-after-update", remote_session_id)

        # 仅向真实 B 的正式发现端点注入旧版本声明，不模拟旧路由或改响应正文。
        native = network.restart_gateway(1, updated_port, {"BOXTEAM_TEST_TARGET_FEDERATION_PROTOCOL_VERSION": "1"})
        for endpoint in ("manifest", "workspaces"):
            old_protocol = exchange(native, "GET", f"/api/gateway/federation/{endpoint}", f"recovery-old-{endpoint}", headers={
                "X-BoxTeam-Federation-Token": current_credential.token,
            })
            assert old_protocol.status_code == 200, old_protocol.text
            assert old_protocol.json()["data"]["protocol_version"] == 1
            assert old_protocol.json()["data"]["gateway_id"] == gateway_b
        incompatible = exchange(local, "POST", f"/api/gateway/remote-gateways/{connection_id}/reconnect", "recovery-old-rejected")
        assert incompatible.status_code == 502, incompatible.text
        assert incompatible.json()["detail"] == "远端 Gateway 连接重连失败: stage=gateway_connection_reconnect, error=RuntimeError"
        diagnostic, = [record for record in re.split(
            r"(?m)(?=^\d{4}-\d{2}-\d{2}T)",
            (network.observation_paths["gateway-a"].parent / "gateway.stderr.log").read_bytes().decode(
                "utf-8", errors="replace",
            ),
        ) if 'logger=app.core.failures request_id="recovery-old-rejected"' in record.partition("\n")[0]]
        assert diagnostic.rstrip().endswith("RuntimeError: 远程 Gateway 协议不兼容: local=2, remote=1")
        evidence.append({"phase": "incompatible-protocol-diagnostic", "diagnostic": diagnostic})
        rejected_target, rejected_connection = projection_snapshot("incompatible-rejected")
        for field in ("target_generation", "runtime_lease_id", "backend_url"):
            assert rejected_target[field] == updated_target[field]
        assert rejected_connection["remote_config_event_cursor"] == updated_connection["remote_config_event_cursor"]
        native = network.restart_gateway(1, updated_port, {})
        compatible = exchange(local, "POST", f"/api/gateway/remote-gateways/{connection_id}/reconnect", "recovery-compatible-again")
        assert compatible.status_code == 200, compatible.text
        assert_remote_sessions("recovery-before-offline", remote_session_id, active=True)
        online_target, online_connection = projection_snapshot("before-offline")

        network.stop_gateway(1)
        unavailable = exchange(local, "GET", canonical, "recovery-offline-request", headers=target_headers)
        assert unavailable.status_code == 502, unavailable.text
        assert network.workspace_id in unavailable.json()["detail"]
        for role in ("gateway-b", "backend-a", "backend-b"):
            assert _observations(network.observation_paths[role], "recovery-offline-request") == []
        # B 不可达时 A 仍须从真实持久快照恢复 owner 与用户活动选择。
        local = network.restart_gateway(0, None, {})
        restored = exchange(local, "GET", "/api/gateway/workspaces", "recovery-cold-offline")
        assert restored.status_code == 200, restored.text
        restored_data = restored.json()["data"]
        assert restored_data["active_workspace"] == owner
        offline, = [item for item in restored_data["items"] if item["gateway_id"] == gateway_b]
        assert offline["workspace_id"] == network.workspace_id and offline["active"] is True
        assert offline["status"] == offline["services"]["workspace_api"]["status"] == "offline"
        assert offline["connection_error"] and offline["services"]["workspace_api"]["error"]
        assert [item["gateway_connection_id"] for item in offline["remote_gateways"]] == [connection_id]
        cold_target, cold_connection = projection_snapshot("cold-offline")
        for field in ("target_generation", "runtime_lease_id", "backend_url"):
            assert cold_target[field] == online_target[field]
        assert cold_connection["remote_config_event_cursor"] == online_connection["remote_config_event_cursor"]

        native = network.restart_gateway(1, updated_port, {})
        resumed = exchange(local, "POST", f"/api/gateway/remote-gateways/{connection_id}/reconnect", "recovery-final-reconnect")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["data"]["active_workspace"] == owner
        ready, = [item for item in resumed.json()["data"]["items"] if item["gateway_id"] == gateway_b]
        assert ready["workspace_id"] == network.workspace_id and ready["status"] == "ready"
        assert ready["connection_error"] is None
        assert_remote_sessions("recovery-final-canonical", remote_session_id)
        assert_remote_sessions("recovery-final-active", remote_session_id, active=True)
        projection_snapshot("reconnected")
    finally:
        (network.context.artifacts_dir / "target-recovery-http.json").write_text(json.dumps({
            "http": evidence, "snapshots": snapshots, "gateway_history": network.gateway_history,
            "protocol_boundary": "仅目标进程正式 manifest/workspaces 的版本常量为1；不是完整旧版本服务",
            "restart_boundary": "watcher 生成 intent 后显式启动候选并在 promotion 后停旧进程；未覆盖 Launcher 自动协调或其公开监听器",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
