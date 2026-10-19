"""独立 Gateway 与真实 localhost SSH -L 的进程装具，支持共享唯一 Backend。"""

from __future__ import annotations

import asyncio
import getpass
import json
import shlex
import shutil
import sqlite3
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import commentjson
import httpx

from app.core.mounted_workspaces import MountedWorkspace
from app.core.workspace_identity import (
    load_or_create_workspace_id,
    workspace_identity_path,
)
from app.gateway.runtime.process import ManagedProcess
from configs.installer import install_user_configuration
from tests.e2e.system.gateway.gateway_target import GatewaySshTarget, wait_for_ssh_ready
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    start_terminal_backend,
)
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.ports import E2EPortBlock, integration_port_block_for_file
from tests.support.process_scope import register_test_process, unregister_test_process
from tests.support.processes import (
    BackendProcess,
    close_backend_process,
    ensure_local_port_available,
    resolve_workspace_python_executable,
    start_backend_process,
    terminate_process,
)


@dataclass
class CommunicationNode:
    workspace: Path
    client: httpx.AsyncClient
    gateway_id: str
    workspace_id: str
    backend: BackendProcess
    backend_start: Callable[[], BackendProcess]
    gateway_home: Path | None = None
    gateway: GatewayProcess | None = None

    def restart_backend(self, operation: str = "restart") -> dict[str, object]:
        """pytest 只终止本 node 持有的 Popen；重启复用原 mount/composition 与端口。"""
        if operation not in {"restart", "kill", "start"}:
            raise ValueError(f"未知 owned backend 操作: {operation}")
        previous = self.backend
        if operation in {"restart", "kill"}:
            if previous.process.poll() is not None:
                raise RuntimeError("owned backend 已停止，拒绝重复 kill")
            previous.process.kill()
            previous.process.wait(timeout=10)
            close_backend_process(previous)
        if operation in {"restart", "start"}:
            if previous.process.poll() is None:
                raise RuntimeError("owned backend 仍运行，拒绝重复 start")
            self.backend = self.backend_start()
        return {"before_pid": previous.process.pid, "after_pid": self.backend.process.pid if operation != "kill" else None, "port": self.backend.port}

    def close_backend(self) -> None:
        close_backend_process(self.backend)

    @property
    def gateway_root(self) -> Path:
        home = self.gateway_home or self.workspace.parent / "boxteam-home"
        return home / "state/gateway"

    def channels(self) -> list[tuple[str, str]]:
        database = self.gateway_root / "federation/control.sqlite"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            return connection.execute("SELECT peer_gateway_id,direction FROM federation_channel_registry").fetchall()

    async def session_node(self, session_id: str) -> Path:
        response = await self.client.get("/api/v1/session-catalog/export")
        assert response.status_code == 200, response.text
        node = next(item for item in response.json()["data"]["items"] if item["node_id"] == session_id)
        return self.workspace / ".boxteam/sessions" / node["storage_relative_path"]


def _close_ssh(owner: ManagedProcess) -> None:
    owner.close(timeout_seconds=10)
    unregister_test_process(owner)


@asynccontextmanager
async def open_communication_triad(
    context: TestRunContext,
    prepare_workspace: Callable[[TestRunContext, int, int], Path],
    *,
    resource_service_nodes: tuple[str, ...] = (),
    shared_session_ids: tuple[str, ...] = (),
    ports: E2EPortBlock | None = None,
    backend_asgi_app: str = "app.main:app",
    backend_environment: dict[str, str] | None = None,
    gateway_environment: dict[str, str] | None = None,
    on_sshd_started: Callable[[subprocess.Popen[str], Path], None] | None = None,
    same_backend_routes: bool = False,
) -> AsyncIterator[dict[str, CommunicationNode]]:
    ports = ports or integration_port_block_for_file(context.test_file)
    root = context.project_root
    sshd = shutil.which("sshd")
    if sshd is None:
        raise RuntimeError("C3 真实 localhost SSH harness 缺少 sshd")
    ssh_root = context.artifacts_dir / "c3-ssh"
    ssh_root.mkdir(parents=True, exist_ok=True)
    private_key = ssh_root / "private_key"
    shutil.copyfile(root / "asset/gateway_ssh/boxteam_gateway_e2e_ed25519", private_key)
    private_key.chmod(0o600)
    public_key = (root / "asset/gateway_ssh/boxteam_gateway_e2e_ed25519.pub").read_text()
    (ssh_root / "authorized_keys").write_text(public_key)
    known_hosts = ssh_root / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{ports.port(60)} " + " ".join(public_key.split()[:2]) + "\n")
    config = ssh_root / "sshd_config"
    config.write_text("\n".join([
        f"Port {ports.port(60)}", "ListenAddress 127.0.0.1", f"HostKey {private_key}",
        f"AuthorizedKeysFile {ssh_root / 'authorized_keys'}", f"PidFile {ssh_root / 'sshd.pid'}",
        "UsePAM no", "PasswordAuthentication no", "KbdInteractiveAuthentication no",
        "PubkeyAuthentication yes", "StrictModes no", "AllowTcpForwarding yes", "LogLevel VERBOSE", "",
    ]))
    ensure_local_port_available(ports.port(60), service_name="C3 隔离 sshd")
    async with AsyncExitStack() as resources:
        log = resources.enter_context((ssh_root / "sshd.log").open("w"))
        process = await asyncio.to_thread(subprocess.Popen, [sshd, "-D", "-e", "-f", str(config)], stdout=log, stderr=log, text=True, start_new_session=True)
        ssh_owner = ManagedProcess(process=process, log_file=log)
        register_test_process(ssh_owner, lambda: _close_ssh(ssh_owner))
        resources.push_async_callback(asyncio.to_thread, _close_ssh, ssh_owner)
        if on_sshd_started is not None:
            on_sshd_started(process, config)
        username = getpass.getuser()
        target = GatewaySshTarget("communication-localhost", "linux", "127.0.0.1", ports.port(60), username, private_key, known_hosts, str(root), str(root / ".venv"))
        await asyncio.to_thread(wait_for_ssh_ready, target)
        workspaces = {}
        backends = {}
        backend_starters = {}
        nodes = {}
        layout = (("a", 0), ("b", 20)) if same_backend_routes else (("a", 0), ("b", 20), ("c", 40))
        gateway_homes = {name: context.workspace_root / name / "boxteam-home" for name, _ in layout}
        for name, offset in layout:
            if same_backend_routes and name == "b":
                # 两条 Gateway 路由共享唯一 Popen；绝不启动第二个 backend 共写工作区。
                workspaces[name], backends[name] = workspaces["a"], backends["a"]
                backend_starters[name] = backend_starters["a"]
                if gateway_homes[name].exists():
                    shutil.rmtree(gateway_homes[name])
                continue
            node_context = replace(context, workspace_root=context.workspace_root / name / "workspace")
            workspace = prepare_workspace(node_context, ports.port(10), ports.port(offset + 1))
            # 模板身份不能被三个独立工作区共享；由正式启动 owner 创建各自 UUID。
            workspace_identity_path(workspace).unlink()
            for session_id in shared_session_ids:
                seed_catalog_session_bundle(workspace / ".boxteam/sessions", session_id,
                    title="显式歧义测试资源")
            reset_gateway_persistent_state(workspace_root=workspace)
            environment = (backend_environment or {}) | {"BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(offset + 1)}"}
            terminal_url = None
            browser_url = None
            if name in resource_service_nodes:
                terminal_port = ports.port(offset + 4)
                browser_port = ports.port(offset + 5)
                browser_frontend_port = ports.port(offset + 6)
                for service_port in (terminal_port, browser_port, browser_frontend_port):
                    ensure_local_port_available(service_port, service_name="会话信息正式辅助服务")
                terminal = await asyncio.to_thread(
                    start_terminal_backend, backend_port=terminal_port, workspace_root=str(workspace),
                )
                register_test_process(terminal, lambda process=terminal: terminate_process(process))
                resources.push_async_callback(asyncio.to_thread, unregister_test_process, terminal)
                resources.push_async_callback(asyncio.to_thread, terminate_process, terminal)
                browser = await asyncio.to_thread(
                    start_browser_manager_processes, workspace_root=workspace,
                    backend_port=browser_port, frontend_port=browser_frontend_port,
                    backend_host="127.0.0.1",
                )
                for process in (browser.backend_process, browser.frontend_process):
                    register_test_process(process, lambda process=process: terminate_process(process))
                    resources.push_async_callback(asyncio.to_thread, unregister_test_process, process)
                resources.push_async_callback(asyncio.to_thread, close_browser_manager_processes, browser)
                terminal_url = f"http://127.0.0.1:{terminal_port}"
                browser_url = f"http://127.0.0.1:{browser_port}"
            mount = MountedWorkspace(
                    load_or_create_workspace_id(workspace), workspace.resolve(),
                    terminal_backend_url=terminal_url, browser_backend_url=browser_url,
            )
            def start_owned_backend(workspace=workspace, offset=offset, environment=environment, mount=mount, name=name):
                try:
                    return start_backend_process(
                        workspace_root=str(workspace), port=ports.port(offset),
                        log_name="c3-backend", env_overrides=environment,
                        startup_mount=mount, asgi_app=backend_asgi_app,
                        refresh_user_configuration=False,
                    )
                except BaseException as startup_error:
                    startup_error.add_note(f"owned backend 节点: {name}")
                    raise
            backend = await asyncio.to_thread(start_owned_backend)
            workspaces[name], backends[name] = workspace, backend
            backend_starters[name] = start_owned_backend
            def close_owned_backend(name=name):
                close_backend_process(nodes[name].backend if name in nodes else backends[name])
            resources.push_async_callback(asyncio.to_thread, close_owned_backend)
        for name, offset in (*layout[1:], layout[0]):
            workspace = workspaces[name]
            # B 在单 Backend 模式没有启动 backend，仍必须独立初始化自己的 Gateway HOME。
            install_user_configuration(config_root=gateway_homes[name] / "config", profile="default", project_root=root, force=False)
            if name == "a":
                config_path = gateway_homes[name] / "config/gateway.jsonc"
                payload = commentjson.loads(config_path.read_text())
                python = resolve_workspace_python_executable(root)
                payload["workspaces"] = [{
                    "kind": "remote_gateway", "connection_id": "rgw_c3_" + peer, "name": "C3 " + peer,
                    "host": "127.0.0.1", "port": ports.port(60), "username": username, "private_key_path": str(private_key),
                    "remote_gateway_port": ports.port(peer_offset + 1), "activate": False,
                    "remote_pair_command": f"cd {shlex.quote(str(root))} && BOXTEAM_HOME={shlex.quote(str(gateway_homes[peer]))} {shlex.quote(str(python))} -m app.gateway.federation_pairing",
                } for peer, peer_offset in layout[1:]]
                config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            gateway = await asyncio.to_thread(
                start_gateway_process, workspace_root=workspace, port=ports.port(offset + 1),
                default_backend_url=f"http://127.0.0.1:{backends[name].port}",
                ssh_tunnel_port_range=(ports.port(70), ports.port(79)) if name == "a" else None,
                extra_env=(gateway_environment or {}) | {"BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE": str(known_hosts)},
                boxteam_home=gateway_homes[name],
                artifacts_dir=context.artifacts_dir / ("gateway-" + name),
            )
            resources.push_async_callback(asyncio.to_thread, close_gateway_process, gateway)
            client = await resources.enter_async_context(httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=90))
            await acquire_gateway_guest(client)
            health = await client.get("/api/gateway/health")
            assert health.status_code == 200, health.text
            nodes[name] = CommunicationNode(workspace, client, health.json()["data"]["gateway_id"], backends[name].workspace_id, backends[name], backend_starters[name], gateway_homes[name], gateway)
        async with asyncio.timeout(20):
            while len(nodes["a"].channels()) != len(layout) - 1 or any(len(nodes[name].channels()) != 1 for name, _ in layout[1:]):
                await asyncio.sleep(0.05)
        assert sorted(nodes["a"].channels()) == sorted((nodes[name].gateway_id, "outbound") for name, _ in layout[1:])
        for name, _ in layout[1:]:
            assert nodes[name].channels() == [(nodes["a"].gateway_id, "inbound")]
        assert len({node.workspace_id for node in nodes.values()}) == (1 if same_backend_routes else 3)
        assert len({id(node.backend.process) for node in nodes.values()}) == (1 if same_backend_routes else 3)
        yield nodes
