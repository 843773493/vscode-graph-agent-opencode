"""Backend 进程 owner、原宿主协议与真实扩展；主线程边界仍为明确替身。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.api.extension_host import router as backend_host_router
from app.backend_lifecycle import BackendLifespan
from app.container import WorkspaceContainerRegistry
from app.core.backend_identity import load_or_create_backend_id
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import USER_ACCESS_COOKIE_NAME, UserAccessService
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.main import gateway_http_exception_handler
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.extension_management import GatewayExtensionManagement
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.extension_host import router as gateway_host_router
from app.gateway.server.extension_management import router as gateway_management_router
from app.schemas.gateway.extension_host import ExtensionHostInitializationDTO
from app.services.infrastructure.extension_host.owner import (
    NodeHostIdentity,
    _HostSession,
)
from app.services.infrastructure.extension_host.process import NodeExtensionHost
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def node_host_runtime() -> tuple[str, str]:
    executable = os.environ.get("BOXTEAM_TEST_NODE_HOST_EXECUTABLE")
    modules = os.environ.get("BOXTEAM_TEST_NODE_HOST_MODULES")
    if not executable or not modules:
        pytest.skip("UNMET_PREREQUISITE: 需显式提供匹配固定上游的 Node runtime 和已安装宿主依赖")
    assert Path(executable).is_file(), f"Node runtime 不存在：{executable}"
    assert Path(modules).is_dir(), f"Node 宿主依赖不存在：{modules}"
    return executable, modules


@pytest.mark.parametrize("transport", ["ipc", "gateway", "relay"])
async def test_original_node_host_protocol_and_dual_native_extensions(
    node_host_runtime: tuple[str, str], monkeypatch: pytest.MonkeyPatch, transport: str,
    request: pytest.FixtureRequest, network_scope: AsyncExitStack,
    serve_extension_application: Callable[[FastAPI], Awaitable[str]],
) -> None:
    # Python 与 Node 共用运行身份，避免各自按进程 PID 建立不同证据目录。
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", f"node-host-{uuid4()}")
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    executable, modules = node_host_runtime
    if sys.platform not in {"linux", "win32"}:
        pytest.skip("UNMET_PREREQUISITE: 目标私有 IPC 尚未接入此平台")
    handlers: list[asyncio.Task[None]] = []
    hosts: dict[str, NodeExtensionHost] = {}
    manifest = context.run_root / "runtime-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "distribution": "source-development", "version": "0.0.1",
        "python_executable": sys.executable, "application_root": str(context.project_root),
        "config_resources": {key: str(context.project_root / "configs" / f"{key}.jsonc")
                             for key in ("gateway_inline", "gateway_schema", "workspace_inline", "workspace_schema")},
        "skill_resources": str(context.project_root / "resources/skills"),
        "node": {"source": "launcher", "executable": None},
        "extension_host": {"node_executable": executable, "component_root": "runtime/host"},
    }), encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_RUNTIME_MANIFEST", str(manifest))
    home = context.boxteam_home_for_node(request.node.nodeid)
    home.mkdir(parents=True)
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    workspace_ids = {label: str(uuid4()) for label in (
        "A", "B", "wrong-commit", "disconnect", "crash", "backend-shutdown")}
    mounts = []
    for label, workspace_id in workspace_ids.items():
        root = context.workspace_root / context.run_root.name / label
        root.mkdir(parents=True)
        mounts.append(MountedWorkspace(workspace_id, root))
    mounted = MountedWorkspaceRegistry(mounts)
    backend_id = load_or_create_backend_id(home)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(backend_host_router)
    lifecycle = BackendLifespan(None, application)
    registry: WorkspaceContainerRegistry | None = None
    management: GatewayExtensionManagement | None = None
    identities: dict[str, NodeHostIdentity] = {}
    channel_bindings: dict[str, dict[str, object]] = {}
    network = network_scope
    http = await network.enter_async_context(httpx.AsyncClient(timeout=10))

    gateway_id = "gateway_" + "a" * 32
    token = get_gateway_local_token()
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
    gateway_url = ""
    cookie = ""
    backend_url = await serve_extension_application(application)
    gateway = FastAPI()
    gateway.add_middleware(TraceMiddleware)
    gateway.include_router(gateway_host_router)
    gateway.include_router(gateway_management_router)
    gateway.exception_handler(HTTPException)(gateway_http_exception_handler)
    gateway.state.extension_management = None
    gateway_registry = GatewayWorkspaceRegistry(storage_path=home / "workspaces.json", gateway_id=gateway_id)
    network.callback(gateway_registry.close)
    for mount in mounts:
        gateway_registry.upsert(WorkspaceTarget(gateway_id=gateway_id, workspace_id=mount.workspace_id,
            name="same-name", root_path=str(mount.workspace_root), backend_url=backend_url, connection_kind="local"), activate=False)
    state = GatewayStateStore(path=home / "gateway.sqlite")
    network.callback(state.close)
    users = UserAccessService(state=state)
    users.create_user(display_name="Node host", user_id="node-host")
    access = users.acquire_user(user_id="node-host", client_label="protocol-client")
    cookie = f"{USER_ACCESS_COOKIE_NAME}={access.access_session_id}"
    gateway.state.registry = gateway_registry
    gateway.state.user_access_service = users
    gateway.state.federation_policy_store = FederationPolicyStore()
    gateway.state.http_client = http
    gateway_url = (await serve_extension_application(gateway)).replace("http://", "ws://")
    monkeypatch.setenv("BOXTEAM_GATEWAY_URL", gateway_url.replace("ws://", "http://"))
    if transport == "relay":
        relay_id = "gateway_" + "b" * 32
        relay_registry = GatewayWorkspaceRegistry(storage_path=home / "relay-workspaces.json", gateway_id=relay_id)
        network.callback(relay_registry.close)
        credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
            connection_id="native-host", peer_gateway_id=relay_id)
        relay_registry.upsert_remote_gateway(RemoteGatewayConnection(
            connection_id=credential.connection_id, name="native", host="127.0.0.1", port=22,
            username="fixture", private_key_path=None, ssh_config_host=None, remote_gateway_port=1,
            remote_gateway_id=gateway_id, protocol_version=2, source_owner="manual"),
            runtime=WorkspaceRuntime(service_urls={"workspace_api": gateway_url.replace("ws://", "http://")}))
        for mount in mounts:
            relay_registry.upsert(WorkspaceTarget(gateway_id=gateway_id, workspace_id=mount.workspace_id,
                name="same-name", root_path=None, backend_url="", connection_kind="remote_gateway",
                owner="remote_projection", remote_gateway_connection_ids=(credential.connection_id,),
                remote_service_names=("workspace_api",)), activate=False)
        gateway.state.federation_policy_store.publish({"rules": [{"capability": "extension_host_manage", "effect": "allow",
            "principal_ref": principal_ref_for_gateway(relay_id)}]})
        relay = FastAPI()
        relay.add_middleware(TraceMiddleware)
        relay.include_router(gateway_host_router)
        relay.state.registry = relay_registry
        relay.state.user_access_service = users
        relay.state.federation_policy_store = FederationPolicyStore()
        relay.state.http_client = http
        gateway_url = (await serve_extension_application(relay)).replace("http://", "ws://")

    async def forward(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal registry, management
        try:
            # 测试控制头不进入宿主；后面的字节原样传递，不编造 PersistentProtocol 帧。
            label = (await reader.readline()).decode().strip()
            if label == "shutdown-owner":
                await lifecycle.close()
                writer.write(b"closed\n")
                await writer.drain()
                return
            assert label in workspace_ids
            assert label not in hosts
            if registry is None:
                # Node before 已构建固定组件；Gateway只启动这一个原管理实例。
                management = GatewayExtensionManagement.from_environment(home, logging_startup={
                    "revision": "node-host-fixture", "level": "info", "pretty": False,
                })
                assert management is not None
                await management.start()
                gateway.state.extension_management = management
                network.push_async_callback(management.close)
                registry = WorkspaceContainerRegistry(mounted_workspaces=mounted, backend_id=backend_id)
                lifecycle.registry = registry
                application.state.workspace_container_registry = registry
                application.state.workspace_lifespan = lifecycle
            if transport != "ipc":
                discovery = await http.post(f"{gateway_url.replace('ws://', 'http://')}/api/gateway/extension-hosts/resolve",
                    headers={"X-Local-Token": token, "Cookie": cookie, "X-Request-ID": f"discover-{label}"},
                    json={"protocol_version": 1, "gateway_id": gateway_id, "workspace_ids": [workspace_ids[label]]})
                assert discovery.status_code == 200, discovery.text
                target = discovery.json()["data"]
                assert discovery.headers["X-Request-ID"] == discovery.json()["request_id"] == f"discover-{label}"
                assert target["gateway_id"] == gateway_id and target["backend_id"] == backend_id
                assert target["workspace_ids"] == [workspace_ids[label]]
                assert target["source"] == "live" and target["channel_state"] == "available"
                assert target["runtime"]["node_version"] == "24.22.1"
                # 能力读取不得创建任何新的宿主 generation，实际启动仍由原通道完成。
                assert all(session.identity.view_id != label for session in registry.extension_hosts._sessions.values())
                channel_bindings[f"{label}-discovery"] = discovery.json()
                async with connect(f"{gateway_url}/api/gateway/extension-hosts/channel?token={token}",
                        additional_headers={"Cookie": cookie, "X-Request-ID": f"request-{label}"}, compression=None) as channel:
                    await channel.send(json.dumps({"protocol_version": 1, "gateway_id": gateway_id,
                        "backend_id": target["backend_id"], "view_id": label, "workspace_ids": target["workspace_ids"]}))
                    binding = json.loads(await channel.recv())
                    assert binding["request_id"] == f"request-{label}"
                    assert binding["gateway_id"] == gateway_id
                    assert binding["backend_id"] == backend_id
                    assert binding["view_id"] == label and binding["workspace_ids"] == [workspace_ids[label]]
                    session = registry.extension_hosts._sessions[binding["generation"]]
                    assert isinstance(session, _HostSession)
                    assert session.host is not None
                    hosts[label], identities[label] = session.host, session.identity
                    channel_bindings[label] = binding
                    writer.write(json.dumps(binding["initialization"]).encode() + b"\n")
                    await writer.drain()

                    async def to_channel() -> None:
                        while data := await reader.read(65536):
                            await channel.send(data)

                    async def from_channel() -> None:
                        try:
                            async for data in channel:
                                assert isinstance(data, bytes)
                                writer.write(data)
                                await writer.drain()
                        except ConnectionClosed as error:
                            # 原TCP装具以EOF表示宿主退出；保留WS关闭原件，并由原断言核PID与实际返回码。
                            assert label in {"crash", "backend-shutdown"}
                            channel_bindings[f"{label}-closed"] = {"code": channel.close_code, "reason": channel.close_reason}
                            assert error.rcvd is not None and error.rcvd.code == 1011

                    from app.services.infrastructure.extension_host.channel import (
                        run_channel,
                    )
                    await run_channel(to_channel(), from_channel())
                return
            async with registry.extension_hosts.connect(gateway_id=gateway_id, expected_backend_id=backend_id,
                    client_id="protocol-probe", view_id=label, workspace_ids=(workspace_ids[label],)) as connection:
                hosts[label] = connection.host
                identities[label] = connection.identity
                channel_bindings[label] = {"initialization": connection.initialization.model_dump()}
                writer.write(connection.initialization.model_dump_json().encode() + b"\n")
                await writer.drain()
                upstream = asyncio.create_task(forward(reader, connection.writer))
                downstream = asyncio.create_task(forward(connection.reader, writer))
                try:
                    await asyncio.wait((upstream, downstream), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (upstream, downstream):
                        if not task.done():
                            task.cancel()
                    results = await asyncio.gather(upstream, downstream, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            raise result
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.append(asyncio.create_task(serve(reader, writer)))

    server = await asyncio.start_server(accept, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    supervisor = context.project_root / "app/runtime" / (
        "windows_job.py" if sys.platform == "win32" else "posix_job.py"
    )
    command = [
        sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
        "--timeout-seconds", "45", "--", executable,
        "tests/integration/workspace_services/extension_host/node_host.mjs",
    ]
    try:
        result = await asyncio.to_thread(subprocess.run,
            command, cwd=context.project_root,
            env={**os.environ, "BOXTEAM_TEST_NODE_HOST_EXECUTABLE": executable,
                 "BOXTEAM_TEST_NODE_HOST_MODULES": modules, "BOXTEAM_TEST_HOST_BACKEND_PORT": str(port),
                 "BOXTEAM_TEST_HOST_TRANSPORT": transport},
            capture_output=True, timeout=55, check=False)
    finally:
        server.close()
        for handler in handlers:
            if not handler.done():
                handler.cancel()
        outcomes = await asyncio.gather(*handlers, return_exceptions=True)
        await server.wait_closed()
        await lifecycle.close()
        if management is not None:
            management.assert_healthy()
        await network.aclose()
        if management is not None:
            assert management.drained
            (context.artifacts_dir / "gateway-management-process.json").write_text(json.dumps({
                "backend_id": management.backend_id, "instance": None if management.instance is None
                else management.instance.model_dump(mode="json"), "drained": management.drained,
            }))
        (context.artifacts_dir / "backend-host-processes.json").write_text(json.dumps(
            {label: asdict(host.child.state) for label, host in hosts.items()}, ensure_ascii=False, indent=2) + "\n")
        (context.artifacts_dir / "backend-host-bindings.json").write_text(json.dumps(
            {label: asdict(identity) for label, identity in identities.items()}, indent=2) + "\n")
        (context.artifacts_dir / "gateway-host-bindings.json").write_text(json.dumps(channel_bindings, indent=2) + "\n")
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome
    (context.artifacts_dir / "node-host.stdout.raw").write_bytes(result.stdout)
    (context.artifacts_dir / "node-host.stderr.raw").write_bytes(result.stderr)
    (context.artifacts_dir / "node-host-process.json").write_text(
        json.dumps({"command": command, "exit_code": result.returncode}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="replace")
    receipt = json.loads((context.artifacts_dir / "node-host-checks.json").read_text(encoding="utf-8"))
    receipt_hosts = {host["label"]: host for host in receipt["receipts"]}
    assert receipt_hosts["A"]["result"]["native"] == receipt_hosts["A"]["result"]["api"] == "A"
    assert receipt_hosts["B"]["result"]["native"] == receipt_hosts["B"]["result"]["api"] == "B"
    assert receipt_hosts["A"]["errors"] == receipt_hosts["B"]["errors"] == []
    states = json.loads((context.artifacts_dir / "backend-host-processes.json").read_text())
    assert set(states) == set(workspace_ids)
    assert all(state["exit_verified"] for state in states.values())
    assert {label: state["returncode"] for label, state in states.items()} == {
        "A": 0, "B": 0, "wrong-commit": 55, "disconnect": 0,
        "crash": 1 if sys.platform == "win32" else -9, "backend-shutdown": 0,
    }
    assert len({state["pid"] for state in states.values()}) == len(states)
    assert lifecycle.closed and application.state.workspace_container_registry is None
    assert registry is not None and registry._extension_hosts is not None and not registry._extension_hosts.active
    assert len({identity.generation for identity in identities.values()}) == len(workspace_ids)
    for label, identity in identities.items():
        assert (identity.gateway_id, identity.backend_id, identity.view_id) == (
            gateway_id, backend_id, label)
        if transport == "ipc":
            assert identity.client_id == "protocol-probe"
        elif transport == "gateway":
            assert len(identity.client_id) == 64
        else:
            assert identity.client_id.startswith("peer:gateway_" + "b" * 32 + ":")
            assert len(identity.client_id.rsplit(":", 1)[1]) == 64
        assert identity.workspace_ids == (workspace_ids[label],)
        initialization = ExtensionHostInitializationDTO.model_validate(channel_bindings[label]["initialization"])
        assert initialization.parent_pid == os.getpid()
        assert initialization.os == ("windows" if sys.platform == "win32" else "linux")
        assert [folder.model_dump() for folder in initialization.workspace.folders] == [{
            "workspace_id": workspace_ids[label], "native_uri": mounted.resolve(workspace_ids[label]).workspace_root.as_uri(),
            "resource_uri": f"boxteam://workspace/{workspace_ids[label]}/resources/files/root", "name": label}]
        assert initialization.logs_location_uri == (home / "state/backend/extension-hosts" / identity.generation).as_uri()
        assert (home / "state/backend/extension-hosts" / identity.generation / "host.log").is_file()
