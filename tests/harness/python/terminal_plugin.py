"""把正式 Terminal 插件服务挂到测试 Gateway 的 HTTP/WS 路由。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import commentjson
import httpx
import uvicorn
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.auth import get_gateway_local_token
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.plugins.packages import PluginPackageStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.service_plugins.runtime import (
    GatewayPluginRuntime,
    plugin_runtime_lifespan,
)
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.gateway.server.service_plugin_proxy import (
    router as service_plugin_proxy_router,
)
from app.gateway.server.service_plugins import router as service_plugins_router
from app.schemas.gateway.service_plugin import PluginBinding
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)
from configs.plugin_parameters import PluginParameters, plugin_parameters
from tests.support.processes.workspace import ensure_local_port_available
from tests.support.terminal_plugin import copy_prepared_developer_tools_package

PACKAGE_ID = "boxteam.developer-tools"
PACKAGE_VERSION = "0.1.0"
PLUGIN_ID = "terminal"
PLUGIN_IDS = frozenset({"terminal", "browser", "debugging"})


@dataclass(frozen=True, slots=True)
class TerminalPluginConfigSnapshot:
    """从真实 ConfigService 读出的绑定；Gateway owner 只消费该快照。"""

    workspace_id: str
    revision: str
    bindings: tuple[PluginBinding, ...]
    referenced_bindings: tuple[PluginBinding, ...]
    parameters: PluginParameters


class ConfigSnapshotReader(Protocol):
    def __call__(self, workspace_root: Path, /) -> TerminalPluginConfigSnapshot: ...


@dataclass(frozen=True, slots=True)
class TerminalPluginGatewayContext:
    workspace_root: Path
    artifacts_dir: Path
    boxteam_home: Path


@dataclass(slots=True)
class TerminalPluginGatewayComponents:
    gateway_id: str
    workspace_id: str
    registry: GatewayWorkspaceRegistry
    runtime: GatewayPluginRuntime
    read_config_snapshot: ConfigSnapshotReader

    async def apply(self, snapshot: TerminalPluginConfigSnapshot) -> None:
        if snapshot.workspace_id != self.workspace_id:
            raise ValueError("Terminal 插件配置快照不属于当前 Workspace")
        active = list(snapshot.bindings)
        if any(binding.workspace_id != self.workspace_id for binding in active):
            raise ValueError("Terminal 插件绑定必须属于当前 Workspace")
        await self.runtime.apply_scope(
            active,
            revision=snapshot.revision,
            workspace_id=self.workspace_id,
            parameters=snapshot.parameters,
        )

    async def apply_config_service(self, config_service: ConfigService) -> None:
        await self.apply(self.read_config_snapshot_from_service(config_service, self.workspace_id))

    @staticmethod
    def read_config_snapshot_from_service(
        config_service: ConfigService,
        workspace_id: str,
    ) -> TerminalPluginConfigSnapshot:
        active, referenced = config_service.get_workspace_plugin_binding_snapshot()
        return TerminalPluginConfigSnapshot(
            workspace_id=workspace_id,
            revision=config_service.get_revision(),
            bindings=tuple(active),
            referenced_bindings=tuple(referenced),
            parameters=plugin_parameters(config_service.get_snapshot().to_dict()),
        )


@dataclass(slots=True)
class TerminalPluginGateway:
    gateway_url: str
    gateway_id: str
    workspace_id: str
    workspace_headers: dict[str, str]
    instance: dict[str, object]
    client: httpx.AsyncClient
    context: TerminalPluginGatewayContext
    logs_dir: Path
    environment: dict[str, str]
    runtime: GatewayPluginRuntime
    binding: PluginBinding
    revision: str

    @property
    def http_service_url(self) -> str:
        instance_id = str(self.instance["instance_id"])
        plugin_id = self.binding.plugin_id
        query = urlencode({"package_version": PACKAGE_VERSION, "workspace_id": self.workspace_id})
        return (
            f"{self.gateway_url}/api/gateway/service-plugins/{PACKAGE_ID}/{plugin_id}"
            f"/instances/{instance_id}/services/http?{query}"
        )

    @property
    def websocket_url(self) -> str:
        instance_id = str(self.instance["instance_id"])
        plugin_id = self.binding.plugin_id
        service_name = "browser" if plugin_id == "browser" else "terminal"
        query = urlencode({"package_version": PACKAGE_VERSION, "workspace_id": self.workspace_id})
        return (
            f"ws://127.0.0.1:{urlsplit(self.gateway_url).port}/api/gateway/service-plugins/"
            f"{PACKAGE_ID}/{plugin_id}/instances/{instance_id}/services/socket/{service_name}?{query}"
        )

    async def restart(self) -> None:
        """经原 Gateway 插件 owner 重启精确绑定。"""

        await self.runtime.apply_binding(self.binding, revision=self.revision, restart=True,
            parameters=self.runtime.slots[self.runtime.key(self.binding)].parameters)
        slot = self.runtime.slots[(self.binding.package_id, self.binding.plugin_id, self.workspace_id)]
        if slot.receipt is None or slot.state != "ready":
            raise RuntimeError(f"Terminal 插件重启未 ready: {slot.error}")
        self.instance = slot.receipt.model_dump(mode="json", exclude={"log_sink"})


def prepare_terminal_plugin_configuration(
    workspace_root: Path, gateway_url: str, *, plugin_ids: frozenset[str] = frozenset({PLUGIN_ID}),
) -> None:
    """为隔离测试 Workspace 预置启用绑定，保留其余现有配置内容。"""

    config_path = workspace_root / ".boxteam" / "workspace.jsonc"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = commentjson.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    if not isinstance(config, dict):
        raise TypeError(f"Workspace 配置根节点必须为对象: {config_path}")
    runtime_config = config.setdefault("runtime", {})
    if not isinstance(runtime_config, dict):
        raise TypeError("runtime 必须是对象")
    gateway_config = runtime_config.setdefault("gateway", {})
    if not isinstance(gateway_config, dict):
        raise TypeError("runtime.gateway 必须是对象")
    connection = gateway_config.setdefault("connection", {})
    if not isinstance(connection, dict):
        raise TypeError("runtime.gateway.connection 必须是对象")
    connection["url"] = gateway_url
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise TypeError("plugins 必须是对象")
    runtime = plugins.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        raise TypeError("plugins.runtime 必须是对象")
    entries = runtime.setdefault("entries", [])
    if not isinstance(entries, list):
        raise TypeError("plugins.runtime.entries 必须是数组")
    if plugin_ids != frozenset({"debugging"}):
        keys = {(PACKAGE_ID, plugin_id) for plugin_id in plugin_ids}
        selected = [entry for entry in entries if isinstance(entry, dict)
            and (entry.get("package_id"), entry.get("plugin_id")) in keys]
        if len(selected) != len({(entry.get("package_id"), entry.get("plugin_id")) for entry in selected}):
            raise ValueError("测试插件绑定重复")
        entries[:] = [entry for entry in entries if entry not in selected]
        entries.extend({"package_id": PACKAGE_ID, "package_version": PACKAGE_VERSION,
            "plugin_id": plugin_id, "enabled": True} for plugin_id in sorted(plugin_ids))
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_config_service_snapshot(workspace_root: Path) -> TerminalPluginConfigSnapshot:
    workspace_id = load_or_create_workspace_id(workspace_root)
    service = ConfigService(workspace_root=workspace_root)
    active, referenced = service.get_workspace_plugin_binding_snapshot()
    return TerminalPluginConfigSnapshot(
        workspace_id=workspace_id,
        revision=service.get_revision(),
        bindings=tuple(active),
        referenced_bindings=tuple(referenced),
        parameters=plugin_parameters(service.get_snapshot().to_dict()),
    )


def _debug_plugin_snapshot(workspace_id: str) -> TerminalPluginConfigSnapshot:
    return TerminalPluginConfigSnapshot(
        workspace_id=workspace_id,
        revision="debug-plugin-test",
        bindings=(PluginBinding(
            package_id=PACKAGE_ID,
            package_version=PACKAGE_VERSION,
            plugin_id="debugging",
            workspace_id=workspace_id,
            enabled=True,
        ),),
        referenced_bindings=(),
        parameters={},
    )


def build_developer_tools_package(project_root: Path, archive: Path) -> None:
    subprocess.run(
        ["bun", "scripts/plugins/pack-developer-tools.mjs", "--out", str(archive.resolve())],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


@asynccontextmanager
async def install_terminal_plugin_gateway_components(
    *,
    app: FastAPI,
    registry: GatewayWorkspaceRegistry,
    gateway_id: str,
    boxteam_home: Path,
    workspace_root: Callable[[str], Path],
    artifacts_dir: Path,
    workspace_id: str | None = None,
    package_archive: Path | None = None,
    prepared_package_root: Path | None = None,
    read_config_snapshot: ConfigSnapshotReader = _read_config_service_snapshot,
) -> AsyncIterator[TerminalPluginGatewayComponents]:
    """将真实 Terminal 插件 owner 与 Gateway 服务路由装入调用方现有 app。"""

    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("Terminal 插件 Gateway 组件测试需要 PATH 中存在 node")
    node_environment = {key: os.environ.get(key) for key in ("BOXTEAM_NODE_BIN",)}
    try:
        os.environ["BOXTEAM_NODE_BIN"] = node_bin
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        package_store = PluginPackageStore(boxteam_home / "plugins" / "packages")
        if prepared_package_root is not None:
            if package_archive is not None:
                raise ValueError("prepared_package_root 与 package_archive 不能同时提供")
            copy_prepared_developer_tools_package(prepared_package_root, boxteam_home)
        else:
            if package_archive is None:
                package_archive = artifacts_dir / f"{PACKAGE_ID}-{PACKAGE_VERSION}.zip"
                await asyncio.to_thread(build_developer_tools_package, Path.cwd().resolve(), package_archive)
            await asyncio.to_thread(package_store.install_zip, package_archive)
        app.include_router(service_plugin_proxy_router)
        app.include_router(service_plugins_router)
        if not any(middleware.cls is TraceMiddleware for middleware in app.user_middleware):
            app.add_middleware(TraceMiddleware)
        app.state.registry = registry
        app.state.federation_policy_store = FederationPolicyStore()
        async with GatewaySkillSourceOwner(
            gateway_id=gateway_id, skills_root=boxteam_home / "skills", update_policy=SourceUpdatePolicy(mode="manual"),
        ).lifespan() as skill_sources, plugin_runtime_lifespan(
            home=boxteam_home, gateway_id=gateway_id, skill_sources=skill_sources,
            payload=lambda: {"logging": {"runtime": {"level": "WARNING", "pretty": False}}},
            revision=lambda: "service-plugin-test", preserve=lambda: False,
            workspace_root=workspace_root,
        ) as runtime:
            app.state.service_plugins = runtime
            workspace_ids = {target.workspace_id for target in registry.targets()}
            if workspace_id is None:
                if len(workspace_ids) != 1:
                    raise ValueError("Terminal 插件 Gateway 组件需要明确唯一 Workspace")
                workspace_id = next(iter(workspace_ids))
            elif workspace_id not in workspace_ids:
                raise ValueError(f"Terminal 插件 Gateway Registry 未登记 Workspace: {workspace_id}")
            components = TerminalPluginGatewayComponents(
                gateway_id=gateway_id,
                workspace_id=workspace_id,
                registry=registry,
                runtime=runtime,
                read_config_snapshot=read_config_snapshot,
            )
            yield components
    finally:
        _restore_environment(node_environment)


@asynccontextmanager
async def terminal_plugin_gateway_for_workspace(
    workspace_root: Path,
    artifacts_dir: Path,
    gateway_port: int,
    boxteam_home: Path,
    *,
    read_config_snapshot: ConfigSnapshotReader | None = None,
    plugin_ids: frozenset[str] = frozenset({PLUGIN_ID}),
    prepared_package_root: Path | None = None,
) -> AsyncIterator[TerminalPluginGateway]:
    """启动只负责插件代理的测试 Gateway，不创建或启动 Workspace Backend。

    测试装具会直接预置隔离 Workspace 的插件 binding，再由真实 ConfigService
    读取已提交配置并通知真实 GatewayPluginRuntime；产品 CAS 写入口由 Gateway
    lifecycle 集成测试单独覆盖。
    """

    ensure_local_port_available(gateway_port, service_name="Terminal 插件测试 Gateway")
    workspace_root = workspace_root.resolve(strict=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    boxteam_home.mkdir(parents=True, exist_ok=True)
    project_root = Path.cwd().resolve()
    gateway_root = boxteam_home / "state" / "gateway"
    gateway_id = load_or_create_gateway_id(gateway_root / "identity.json")
    workspace_id = load_or_create_workspace_id(workspace_root)
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    if not plugin_ids or not plugin_ids <= PLUGIN_IDS:
        raise ValueError(f"测试 Gateway 仅支持这些插件：{sorted(PLUGIN_IDS)}")
    prepare_terminal_plugin_configuration(workspace_root, gateway_url, plugin_ids=plugin_ids)

    # get_gateway_root() 和插件子进程都必须看见这个隔离 Home。
    env_values = {
        "BOXTEAM_HOME": str(boxteam_home.resolve()),
        "BOXTEAM_GATEWAY_URL": gateway_url,
        "BOXTEAM_GATEWAY_ID": gateway_id,
        "BOXTEAM_NODE_BIN": shutil.which("node") or "",
        "BOXTEAM_PROJECT_ROOT": str(project_root),
    }
    if not env_values["BOXTEAM_NODE_BIN"]:
        raise RuntimeError("Terminal 插件测试需要 PATH 中存在 node")
    previous_environment = {key: os.environ.get(key) for key in (*env_values, "BOXTEAM_GATEWAY_ROOT")}
    with ExitStack() as environment_cleanup:
        environment_cleanup.callback(_restore_environment, previous_environment)
        os.environ.update(env_values)
        os.environ.pop("BOXTEAM_GATEWAY_ROOT", None)
        token_path = gateway_root / "credentials" / "local-token"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("local-dev-token\n", encoding="utf-8")
        token_path.chmod(0o600)
        token = get_gateway_local_token()
        registry = GatewayWorkspaceRegistry(
            storage_path=gateway_root / "terminal-plugin-test-registry.json",
            gateway_id=gateway_id,
        )
        registry.upsert(
            WorkspaceTarget(
                gateway_id=gateway_id,
                workspace_id=workspace_id,
                name="Terminal plugin test workspace",
                root_path=str(workspace_root),
                backend_url="http://127.0.0.1:1",
                connection_kind="local",
                owner="manual",
                managed=False,
                removable=False,
            ),
            activate=False,
        )
        app = FastAPI()
        archive = None
        if prepared_package_root is None:
            archive = artifacts_dir / f"{PACKAGE_ID}-{PACKAGE_VERSION}.zip"
            await asyncio.to_thread(build_developer_tools_package, project_root, archive)
        async with AsyncExitStack() as resources:
            resources.callback(registry.close)
            server = uvicorn.Server(uvicorn.Config(
                app,
                host="127.0.0.1",
                port=gateway_port,
                log_level="warning",
                access_log=False,
                log_config=None,
                lifespan="off",
            ))
            server_task: asyncio.Task[None] | None = None

            async def stop_server() -> None:
                if server_task is not None:
                    server.should_exit = True
                    await asyncio.wait_for(server_task, timeout=10)

            snapshot_reader = read_config_snapshot or _read_config_service_snapshot
            if plugin_ids == frozenset({"debugging"}) and read_config_snapshot is None:
                snapshot_reader = lambda _root: _debug_plugin_snapshot(workspace_id)

            async with install_terminal_plugin_gateway_components(
                app=app,
                registry=registry,
                gateway_id=gateway_id,
                boxteam_home=boxteam_home,
                workspace_root=lambda _workspace_id: workspace_root,
                artifacts_dir=artifacts_dir,
                package_archive=archive,
                prepared_package_root=prepared_package_root,
                workspace_id=workspace_id,
                read_config_snapshot=snapshot_reader,
            ) as components:
                snapshot = components.read_config_snapshot(workspace_root)
                if snapshot.workspace_id != workspace_id:
                    raise ValueError("ConfigService 快照 Workspace 身份与 Gateway Registry 不一致")
                await components.apply(snapshot)
                plugin_id = next(iter(plugin_ids))
                key = (PACKAGE_ID, plugin_id, workspace_id)
                slot = components.runtime.slots[key]
                if slot.receipt is None or slot.state != "ready":
                    raise RuntimeError(f"{plugin_id} 插件未 ready: {slot.error}")
                instance = slot.receipt.model_dump(mode="json", exclude={"log_sink"})
                instance_id = slot.receipt.instance_id
                client = httpx.AsyncClient(
                    base_url=gateway_url,
                    headers={"X-Local-Token": token},
                    timeout=30,
                    trust_env=False,
                )
                resources.push_async_callback(client.aclose)
                context = TerminalPluginGatewayContext(workspace_root, artifacts_dir, boxteam_home)
                owner_name = f"workspace-{plugin_id}"
                handle = TerminalPluginGateway(
                    gateway_url=gateway_url,
                    gateway_id=gateway_id,
                    workspace_id=workspace_id,
                    workspace_headers={
                        "X-Local-Token": token,
                        "X-BoxTeam-Workspace-Id": workspace_id,
                        "X-BoxTeam-Resource-Owner": owner_name,
                    },
                    instance=instance,
                    client=client,
                    context=context,
                    logs_dir=boxteam_home / "state" / "logs",
                    environment={**env_values, "BOXTEAM_GATEWAY_LOCAL_TOKEN": token},
                    runtime=components.runtime,
                    binding=slot.binding,
                    revision=snapshot.revision,
                )
                server_task = asyncio.create_task(server.serve())
                resources.push_async_callback(stop_server)
                async with asyncio.timeout(10):
                    while not server.started:
                        if server_task.done():
                            await server_task
                        await asyncio.sleep(0.01)
                # 安装代理 router 后 Gateway app 在内存中已可用；yield 前确认真实 TCP listener。
                if plugin_id != "debugging":
                    async with httpx.AsyncClient(
                        base_url=gateway_url,
                        headers={"X-Local-Token": token},
                        params={"package_version": PACKAGE_VERSION, "workspace_id": workspace_id},
                        timeout=5,
                        trust_env=False,
                    ) as probe:
                        response = await probe.get(
                            f"/api/gateway/service-plugins/{PACKAGE_ID}/{plugin_id}"
                            f"/instances/{instance_id}/services/http/api/{'browsers' if plugin_id == 'browser' else 'terminals'}",
                            headers={
                                "X-BoxTeam-Workspace-Id": workspace_id,
                                "X-BoxTeam-Resource-Owner": owner_name,
                            },
                        )
                        response.raise_for_status()
                yield handle


@asynccontextmanager
async def browser_plugin_gateway_for_workspace(
    workspace_root: Path,
    artifacts_dir: Path,
    gateway_port: int,
    boxteam_home: Path,
    prepared_package_root: Path,
) -> AsyncIterator[TerminalPluginGateway]:
    """以正式 Gateway 插件运行时启动 Browser 服务。"""

    async with terminal_plugin_gateway_for_workspace(
        workspace_root,
        artifacts_dir,
        gateway_port,
        boxteam_home,
        plugin_ids=frozenset({"browser"}),
        prepared_package_root=prepared_package_root,
    ) as gateway:
        yield gateway


@asynccontextmanager
async def debug_plugin_gateway_for_workspace(
    workspace_root: Path,
    artifacts_dir: Path,
    gateway_port: int,
    boxteam_home: Path,
    prepared_package_root: Path,
) -> AsyncIterator[TerminalPluginGateway]:
    """以正式 Gateway 插件运行时启动 Debug 运行宿主。"""

    async with terminal_plugin_gateway_for_workspace(
        workspace_root,
        artifacts_dir,
        gateway_port,
        boxteam_home,
        plugin_ids=frozenset({"debugging"}),
        prepared_package_root=prepared_package_root,
    ) as gateway:
        yield gateway


def _restore_environment(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
