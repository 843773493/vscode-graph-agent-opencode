"""三个正式隔离 Gateway 与真实 localhost SSH -L 的进程装具。"""

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
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.process_scope import register_test_process, unregister_test_process
from tests.support.processes import (
    close_backend_process,
    ensure_local_port_available,
    resolve_workspace_python_executable,
    start_backend_process,
    terminate_process,
)


@dataclass(frozen=True)
class CommunicationNode:
    workspace: Path
    client: httpx.AsyncClient
    gateway_id: str
    workspace_id: str

    @property
    def gateway_root(self) -> Path:
        return self.workspace.parent / "boxteam-home/state/gateway"

    def channels(self) -> list[tuple[str, str]]:
        database = self.gateway_root / "federation/control.sqlite"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            return connection.execute("SELECT peer_gateway_id,direction FROM federation_channel_registry").fetchall()

    async def session_node(self, session_id: str) -> Path:
        response = await self.client.get("/api/v1/session-catalog/export")
        assert response.status_code == 200, response.text
        node = next(item for item in response.json()["data"]["items"] if item["node_id"] == session_id)
        return self.workspace / ".boxteam/sessions" / node["storage_relative_path"]


def _close_ssh(process: subprocess.Popen[str]) -> None:
    terminate_process(process)
    unregister_test_process(process)


@asynccontextmanager
async def open_communication_triad(
    context: TestRunContext,
    prepare_workspace: Callable[[TestRunContext, int, int], Path],
    *,
    resource_service_nodes: tuple[str, ...] = (),
    shared_session_ids: tuple[str, ...] = (),
) -> AsyncIterator[dict[str, CommunicationNode]]:
    ports = integration_port_block_for_file(context.test_file)
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
        process = await asyncio.to_thread(subprocess.Popen, [sshd, "-D", "-e", "-f", str(config)], stdout=log, stderr=log, text=True)
        register_test_process(process, lambda: _close_ssh(process))
        resources.push_async_callback(asyncio.to_thread, _close_ssh, process)
        username = getpass.getuser()
        target = GatewaySshTarget("communication-localhost", "linux", "127.0.0.1", ports.port(60), username, private_key, known_hosts, str(root), str(root / ".venv"))
        await asyncio.to_thread(wait_for_ssh_ready, target)
        workspaces = {}
        backends = {}
        for name, offset in (("a", 0), ("b", 20), ("c", 40)):
            node_context = replace(context, workspace_root=context.workspace_root / name / "workspace")
            workspace = prepare_workspace(node_context, ports.port(10), ports.port(offset + 1))
            # 模板身份不能被三个独立工作区共享；由正式启动 owner 创建各自 UUID。
            workspace_identity_path(workspace).unlink()
            for session_id in shared_session_ids:
                seed_catalog_session_bundle(workspace / ".boxteam/sessions", session_id,
                    title="显式歧义测试资源")
            reset_gateway_persistent_state(workspace_root=workspace)
            environment = {"BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(offset + 1)}"}
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
            backend = await asyncio.to_thread(
                start_backend_process, workspace_root=str(workspace), port=ports.port(offset),
                log_name="c3-backend", env_overrides=environment,
                startup_mount=MountedWorkspace(
                    load_or_create_workspace_id(workspace), workspace.resolve(),
                    terminal_backend_url=terminal_url, browser_backend_url=browser_url,
                ),
            )
            resources.push_async_callback(asyncio.to_thread, close_backend_process, backend)
            workspaces[name], backends[name] = workspace, backend
        nodes = {}
        for name, offset in (("b", 20), ("c", 40), ("a", 0)):
            workspace = workspaces[name]
            if name == "a":
                config_path = workspace.parent / "boxteam-home/config/gateway.jsonc"
                payload = commentjson.loads(config_path.read_text())
                python = resolve_workspace_python_executable(root)
                payload["workspaces"] = [{
                    "kind": "remote_gateway", "connection_id": "rgw_c3_" + peer, "name": "C3 " + peer,
                    "host": "127.0.0.1", "port": ports.port(60), "username": username, "private_key_path": str(private_key),
                    "remote_gateway_port": ports.port(peer_offset + 1), "activate": False,
                    "remote_pair_command": f"cd {shlex.quote(str(root))} && BOXTEAM_HOME={shlex.quote(str(workspaces[peer].parent / 'boxteam-home'))} {shlex.quote(str(python))} -m app.gateway.federation_pairing",
                } for peer, peer_offset in (("b", 20), ("c", 40))]
                config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            gateway = await asyncio.to_thread(
                start_gateway_process, workspace_root=workspace, port=ports.port(offset + 1),
                default_backend_url=f"http://127.0.0.1:{backends[name].port}",
                ssh_tunnel_port_range=(ports.port(70), ports.port(79)) if name == "a" else None,
                extra_env={"BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE": str(known_hosts)},
            )
            resources.push_async_callback(asyncio.to_thread, close_gateway_process, gateway)
            client = await resources.enter_async_context(httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=90))
            await acquire_gateway_guest(client)
            health = await client.get("/api/gateway/health")
            assert health.status_code == 200, health.text
            nodes[name] = CommunicationNode(workspace, client, health.json()["data"]["gateway_id"], backends[name].workspace_id)
        async with asyncio.timeout(20):
            while len(nodes["a"].channels()) != 2 or len(nodes["b"].channels()) != 1 or len(nodes["c"].channels()) != 1:
                await asyncio.sleep(0.05)
        assert sorted(nodes["a"].channels()) == sorted([(nodes["b"].gateway_id, "outbound"), (nodes["c"].gateway_id, "outbound")])
        assert nodes["b"].channels() == [(nodes["a"].gateway_id, "inbound")]
        assert nodes["c"].channels() == [(nodes["a"].gateway_id, "inbound")]
        assert len({node.workspace_id for node in nodes.values()}) == 3
        yield nodes
