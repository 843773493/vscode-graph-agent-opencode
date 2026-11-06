"""Backend 进程 owner、原宿主协议与真实扩展；主线程边界仍为明确替身。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI

from app.backend_lifecycle import BackendLifespan
from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.services.infrastructure.extension_host.owner import NodeHostIdentity
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


async def test_original_node_host_protocol_and_dual_native_extensions(
    node_host_runtime: tuple[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Python 与 Node 共用运行身份，避免各自按进程 PID 建立不同证据目录。
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", f"node-host-{uuid4()}")
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    executable, modules = node_host_runtime
    if sys.platform != "linux":
        pytest.skip("UNMET_PREREQUISITE: 目标私有 named pipe 尚未实现")
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
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    workspace_ids = {label: str(uuid4()) for label in (
        "A", "B", "wrong-commit", "disconnect", "crash", "backend-shutdown")}
    mounts = []
    for label, workspace_id in workspace_ids.items():
        root = context.workspace_root / context.run_root.name / label
        root.mkdir(parents=True)
        mounts.append(MountedWorkspace(workspace_id, root))
    mounted = MountedWorkspaceRegistry(mounts)
    backend_id = str(uuid4())
    application = FastAPI()
    lifecycle = BackendLifespan(None, application)
    registry: WorkspaceContainerRegistry | None = None
    identities: dict[str, NodeHostIdentity] = {}

    async def forward(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal registry
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
                registry = WorkspaceContainerRegistry(mounted_workspaces=mounted, backend_id=backend_id)
                lifecycle.registry = registry
                application.state.workspace_container_registry = registry
                application.state.workspace_lifespan = lifecycle
            async with registry.extension_hosts.connect(gateway_id="gateway-probe", expected_backend_id=backend_id,
                    client_id="protocol-probe", view_id=label, workspace_ids=(workspace_ids[label],)) as connection:
                hosts[label] = connection.host
                identities[label] = connection.identity
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
                 "BOXTEAM_TEST_NODE_HOST_MODULES": modules, "BOXTEAM_TEST_HOST_BACKEND_PORT": str(port)},
            capture_output=True, timeout=55, check=False)
    finally:
        server.close()
        for handler in handlers:
            if not handler.done():
                handler.cancel()
        outcomes = await asyncio.gather(*handlers, return_exceptions=True)
        await server.wait_closed()
        await lifecycle.close()
        (context.artifacts_dir / "backend-host-processes.json").write_text(json.dumps(
            {label: asdict(host.child.state) for label, host in hosts.items()}, ensure_ascii=False, indent=2) + "\n")
        (context.artifacts_dir / "backend-host-bindings.json").write_text(json.dumps(
            {label: asdict(identity) for label, identity in identities.items()}, indent=2) + "\n")
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
        "A": 0, "B": 0, "wrong-commit": 55, "disconnect": 0, "crash": -9, "backend-shutdown": 0,
    }
    assert len({state["pid"] for state in states.values()}) == len(states)
    assert lifecycle.closed and application.state.workspace_container_registry is None
    assert registry is not None and registry._extension_hosts is not None and not registry._extension_hosts.active
    assert len({identity.generation for identity in identities.values()}) == len(workspace_ids)
    for label, identity in identities.items():
        assert (identity.gateway_id, identity.backend_id, identity.client_id, identity.view_id) == (
            "gateway-probe", backend_id, "protocol-probe", label)
        assert identity.workspace_ids == (workspace_ids[label],)
        assert (context.boxteam_home / "state/backend/extension-hosts" / identity.generation / "host.log").is_file()
