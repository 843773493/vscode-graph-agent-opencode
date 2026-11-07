"""Backend 独占宿主进程及关闭责任；工作区根只解析原 mounted registry。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.core.mounted_workspaces import MountedWorkspaceRegistry
from app.core.resource_uri import parse_resource_uri
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationViewRequest,
)
from app.schemas.gateway.extension_host import (
    ExtensionHostConfigurationResourceDTO,
    ExtensionHostEnvironmentDTO,
    ExtensionHostFolderDTO,
    ExtensionHostInitializationDTO,
    ExtensionHostWorkspaceDTO,
)
from app.schemas.gateway.extension_installation import (
    ExtensionInstallationInstallDTO,
    ExtensionInstallationInstallRequest,
    ExtensionInstallationUninstallDTO,
    ExtensionInstallationUninstallRequest,
    NativeExtensionFileRequest,
    ProfileExtensionInventory,
)
from app.schemas.gateway.extension_web import ExtensionWebHostOpenRequest
from app.services.infrastructure.config.source_uri import extension_configuration_uri
from app.services.infrastructure.extension_host.configuration import (
    ExtensionConfigurationFiles,
)
from app.services.infrastructure.extension_host.management import (
    NodeExtensionManagement,
)
from app.services.infrastructure.extension_host.process import NodeExtensionHost
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


@dataclass(frozen=True, slots=True)
class NodeHostIdentity:
    gateway_id: str
    backend_id: str
    client_id: str
    view_id: str
    workspace_ids: tuple[str, ...]
    generation: str
    host_kind: Literal["node", "web"] = "node"


@dataclass(frozen=True, slots=True)
class NodeHostConnection:
    identity: NodeHostIdentity
    initialization: ExtensionHostInitializationDTO
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    host: NodeExtensionHost
    stopping: asyncio.Event
    transport_settled: asyncio.Event


@dataclass(frozen=True, slots=True)
class WebHostConnection:
    identity: NodeHostIdentity
    inventory: ProfileExtensionInventory
    stopping: asyncio.Event
    transport_settled: asyncio.Event


@dataclass(frozen=True, slots=True)
class NodeHostStatus:
    backend_id: str
    platform: str
    reason: Literal["runtime_missing", "platform_unsupported", "backend_draining"] | None
    runtime: NodeHostRuntime | None


class _HostSession:
    def __init__(self, identity: NodeHostIdentity, *, runtime: NodeHostRuntime, directory: Path,
                 environment: Mapping[str, str], startup_timeout: float) -> None:
        self.identity = identity
        self._runtime, self._directory = runtime, directory
        self._environment, self._startup_timeout = environment, startup_timeout
        self._scope = AsyncExitStack()
        self._lock = asyncio.Lock()
        self._output: BinaryIO | None = None
        self.host: NodeExtensionHost | None = None
        self.configuration: ExtensionConfigurationFiles | None = None
        self.closed = False
        self.stopping = asyncio.Event()
        self.transport_settled = asyncio.Event()
        self.transport_settled.set()

    async def open(self, initialization: ExtensionHostInitializationDTO) -> NodeHostConnection:
        async with self._lock:
            if self.closed:
                raise RuntimeError("Node 宿主 generation 已撤销，不能重新连接")
            self._directory.mkdir(parents=True, exist_ok=False)
            self._output = (self._directory / "host.log").open("xb")
            self.host = NodeExtensionHost(self.identity.generation, runtime=self._runtime,
                directory=self._directory, environment=self._environment,
                output=self._output, startup_timeout=self._startup_timeout)
            reader, writer = await self._scope.enter_async_context(self.host.connect())
            return NodeHostConnection(self.identity, initialization, reader, writer, self.host, self.stopping, self.transport_settled)

    async def close(self) -> None:
        # 通知公开传输先停止收发；随后关闭IPC时不能再写入已关闭的writer。
        self.stopping.set()
        await self.transport_settled.wait()
        async with self._lock:
            if self.closed:
                return
            # 先断开原IPC。失败时保留本记录和日志句柄；重试仍关闭原child，
            # 不能因为async context已经退栈就把未退出进程当作完成。
            await self._scope.aclose()
            if self.host is not None:
                await self.host.child.close()
            if self._output is not None:
                await run_sync_shielded(self._output.close)
            self.closed = True


class _WebHostSession:
    def __init__(self, identity: NodeHostIdentity) -> None:
        self.identity = identity
        self.profile_id: str | None = None
        self.extension_keys: frozenset[str] = frozenset()
        self.closed = False
        self.stopping = asyncio.Event()
        self.transport_settled = asyncio.Event()
        self.transport_settled.set()

    async def close(self) -> None:
        self.stopping.set()
        await self.transport_settled.wait()
        self.closed = True


class NodeExtensionHostOwner:
    """一个 Backend 的惰性宿主集合；内部准入只接受入口已认证的客户端身份。"""

    # TODO(5.4/5.6)：迁齐MainThread初始载荷、事件的工作区投影与窗口贡献代际；
    # 已认证通道和内部集合校验不能代替RPC载荷的视图隔离。

    def __init__(self, *, backend_id: str, mounted_workspaces: MountedWorkspaceRegistry,
                 runtime: NodeHostRuntime | None, home: Path, environment: Mapping[str, str],
                 startup_timeout: float) -> None:
        self.backend_id = backend_id
        self.runtime = runtime
        self._mounts = mounted_workspaces
        self._home = home
        self._environment = dict(environment)
        self._startup_timeout = startup_timeout
        self._sessions: dict[str, _HostSession | _WebHostSession] = {}
        self._admission_closed = False
        self._management: NodeExtensionManagement | None = None
        self._configuration_watch = WorkspaceFileWatchService(workspace_root=home / "state/backend/extension-management" / backend_id)
        self._configuration_closed = True

    @property
    def active(self) -> tuple[NodeHostIdentity, ...]:
        return tuple(session.identity for session in self._sessions.values())

    def close_admission(self) -> None:
        self._admission_closed = True
        if self._management is not None:
            self._management.close_admission()

    @property
    def management(self) -> NodeExtensionManagement:
        if self._admission_closed:
            raise RuntimeError("Backend 正在关闭，拒绝扩展管理")
        if self.runtime is None:
            raise NotImplementedError("目标未提供专用 Node 扩展管理运行时")
        if self._management is None:
            self._management = NodeExtensionManagement(runtime=self.runtime,
                directory=self._home / "state/backend/extension-management" / self.backend_id,
                environment=self._environment, startup_timeout=self._startup_timeout, operation_timeout=120)
        return self._management

    @property
    def drained(self) -> bool:
        return not self._sessions and self._configuration_closed and (self._management is None or self._management.drained)

    def configuration_files(self, request: ExtensionConfigurationViewRequest, *, client_id: str) -> ExtensionConfigurationFiles:
        session = self._sessions.get(request.generation)
        if (self._admission_closed or not isinstance(session, _HostSession) or session.closed or session.stopping.is_set()
            or session.identity != NodeHostIdentity(request.gateway_id, request.backend_id, client_id,
                request.view_id, tuple(request.workspace_ids), request.generation) or session.configuration is None):
            raise ValueError("配置请求不属于当前已认证的宿主代际和明确工作区视图")
        return session.configuration

    def check_web_extension_file(self, request: NativeExtensionFileRequest, *, client_id: str) -> None:
        session = self._sessions.get(request.generation or "")
        if (self._admission_closed or not isinstance(session, _WebHostSession)
            or session.closed or session.stopping.is_set() or request.current_profile_member
            or session.identity != NodeHostIdentity(request.gateway_id, request.backend_id, client_id,
                request.view_id or "", session.identity.workspace_ids, request.generation or "", "web")
            or request.profile_id != session.profile_id or request.extension_key not in session.extension_keys):
            raise ValueError("扩展包请求不属于当前已认证的 Web 宿主代际和冻结版本")

    def describe(self, *, expected_backend_id: str | None, workspace_ids: tuple[str, ...]) -> NodeHostStatus:
        if expected_backend_id is not None and expected_backend_id != self.backend_id:
            raise ValueError("Node 宿主目标 Backend 身份不匹配")
        if not workspace_ids or len(set(workspace_ids)) != len(workspace_ids):
            raise ValueError("Node 宿主工作区视图必须非空且不能重复")
        for workspace_id in workspace_ids:
            self._mounts.resolve(workspace_id)
        reason = ("backend_draining" if self._admission_closed else "runtime_missing" if self.runtime is None
                  else "platform_unsupported" if sys.platform not in {"linux", "win32"} else None)
        return NodeHostStatus(self.backend_id, sys.platform, reason, self.runtime)

    def management_target(self, expected_backend_id: str | None = None, *, require_runtime: bool = False) -> str:
        if expected_backend_id is not None and expected_backend_id != self.backend_id:
            raise ValueError("扩展安装域 Backend 身份不匹配")
        if self._admission_closed:
            raise ValueError("Backend 正在关闭，拒绝扩展管理")
        if require_runtime and self.runtime is None:
            raise ValueError("目标未提供专用扩展管理运行时")
        return self.backend_id

    async def install(self, request: ExtensionInstallationInstallRequest) -> ExtensionInstallationInstallDTO:
        self.management_target(request.backend_id, require_runtime=True)
        for workspace_id in request.workspace_ids:
            self._mounts.resolve(workspace_id)
        source = parse_resource_uri(request.source_uri, local_gateway_id=request.gateway_id)
        mount = self._mounts.resolve(source.scope_id)
        # 沿原文件 owner 解析及保护私有目录；不装配 Agent 容器或另建根绑定。
        paths = WorkspaceFilePaths(workspace=mount.workspace_root, workspace_id=mount.workspace_id, bindings=None)
        path = await run_sync_shielded(lambda: paths.native(source.display_uri))
        publication = await self.management.install(path)
        return ExtensionInstallationInstallDTO(**request.model_dump(exclude={"source_uri", "gateway_connection_id"}),
            installed=publication.installed, inventory=publication.inventory)

    async def uninstall(self, request: ExtensionInstallationUninstallRequest) -> ExtensionInstallationUninstallDTO:
        self.management_target(request.backend_id, require_runtime=True)
        publication = await self.management.uninstall(profile_id=request.profile_id,
            extension_id=request.extension_id, version=request.version)
        return ExtensionInstallationUninstallDTO(**request.model_dump(exclude={"profile_id", "extension_id", "version", "gateway_connection_id"}),
            uninstalled=publication.uninstalled, inventory=publication.inventory)

    async def _release(self, session: _HostSession | _WebHostSession) -> None:
        await session.close()
        self._sessions.pop(session.identity.generation, None)

    async def _initialization(self, identity: NodeHostIdentity, directory: Path) -> ExtensionHostInitializationDTO:
        native_environment = await self.management.environment()
        environment = ExtensionHostEnvironmentDTO(**native_environment.model_dump(exclude={"settings_native_uri"}),
            remote_settings=ExtensionHostConfigurationResourceDTO(native_uri=native_environment.settings_native_uri,
                resource_uri=extension_configuration_uri(gateway_id=identity.gateway_id, backend_id=identity.backend_id,
                                                        profile_id=native_environment.profile_id)))
        folders = []
        for workspace_id in identity.workspace_ids:
            mount = self._mounts.resolve(workspace_id)
            paths = WorkspaceFilePaths(workspace=mount.workspace_root, workspace_id=workspace_id, bindings=None)
            folders.append(ExtensionHostFolderDTO(workspace_id=workspace_id,
                resource_uri=paths.workspace_uri,
                native_uri=mount.workspace_root.as_uri(), name=mount.workspace_root.name or str(mount.workspace_root)))
        # 安装域、明确视图和有序根集合决定 workspaceState 身份；客户端和临时 generation 不参与。
        view = json.dumps([identity.gateway_id, identity.backend_id, identity.view_id, identity.workspace_ids], separators=(",", ":"))
        workspace = ExtensionHostWorkspaceDTO(id=str(uuid5(NAMESPACE_URL, view)),
            name=", ".join(folder.name for folder in folders), folders=folders)
        if len(folders) > 1:
            location = await self.management.prepare_configuration_view(workspace.id,
                [{"native_uri": folder.native_uri, "name": folder.name} for folder in folders])
            workspace = workspace.model_copy(update={"configuration": ExtensionHostConfigurationResourceDTO(
                native_uri=location.native_uri, resource_uri=extension_configuration_uri(gateway_id=identity.gateway_id,
                    backend_id=identity.backend_id, workspace_id=workspace.id))})
        return ExtensionHostInitializationDTO(os="windows" if sys.platform == "win32" else "linux",
            parent_pid=os.getpid(), environment=environment, logs_location_uri=directory.as_uri(), workspace=workspace)

    @asynccontextmanager
    async def connect(self, *, gateway_id: str, expected_backend_id: str, client_id: str,
                      view_id: str, workspace_ids: tuple[str, ...]) -> AsyncIterator[NodeHostConnection]:
        status = self.describe(expected_backend_id=expected_backend_id, workspace_ids=tuple(workspace_ids))
        if status.reason == "backend_draining":
            raise RuntimeError("Backend 正在关闭，拒绝新的 Node 宿主")
        if not all(value.strip() for value in (gateway_id, client_id, view_id)):
            raise ValueError("Node 宿主必须绑定已认证客户端和明确工作区视图")
        workspace_ids = tuple(workspace_ids)
        if status.runtime is None:
            raise NotImplementedError("目标未提供专用 Node Extension Host 运行时")
        # TODO: macOS 私有IPC验收后启用，不能落到其它目标。
        if status.reason == "platform_unsupported":
            raise NotImplementedError("Node Extension Host 当前仅支持 Linux 和 Windows 目标")
        if any((session.identity.host_kind, session.identity.gateway_id, session.identity.client_id, session.identity.view_id)
               == ("node", gateway_id, client_id, view_id) for session in self._sessions.values()):
            raise ValueError("该客户端的 Node 宿主视图尚未关闭；不能覆盖原 generation")
        identity = NodeHostIdentity(gateway_id, self.backend_id, client_id, view_id, workspace_ids, str(uuid4()))
        directory = self._home / "state/backend/extension-hosts" / identity.generation
        session = _HostSession(identity, runtime=status.runtime,
            directory=directory,
            environment=self._environment, startup_timeout=self._startup_timeout)
        # 预留与关闭快照之间没有await；启动中的实例同样归原owner回收。
        self._sessions[identity.generation] = session
        failure: BaseException | None = None
        try:
            initialization = await self._initialization(identity, directory)
            self._configuration_closed = False
            session.configuration = ExtensionConfigurationFiles(initialization, self.management,
                self._configuration_watch, session.stopping)
            connection = await session.open(initialization)
            if self._admission_closed:
                raise RuntimeError("Node 宿主启动期间 Backend 已撤销准入")
            yield connection
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                await run_cleanup_shielded(lambda: self._release(session))
            except BaseException as close_error:
                if failure is not None:
                    raise BaseExceptionGroup("Node 宿主连接及原进程回收均失败", [failure, close_error]) from None
                raise

    @asynccontextmanager
    async def connect_web(self, request: ExtensionWebHostOpenRequest, *, client_id: str) -> AsyncIterator[WebHostConnection]:
        self.management_target(request.backend_id, require_runtime=True)
        if not client_id.strip():
            raise ValueError("Web 宿主必须绑定已认证客户端")
        workspace_ids = tuple(request.workspace_ids)
        for workspace_id in workspace_ids:
            self._mounts.resolve(workspace_id)
        if any((session.identity.host_kind, session.identity.gateway_id, session.identity.client_id, session.identity.view_id)
               == ("web", request.gateway_id, client_id, request.view_id) for session in self._sessions.values()):
            raise ValueError("该客户端的 Web 宿主视图尚未关闭；不能覆盖原 generation")
        identity = NodeHostIdentity(request.gateway_id, self.backend_id, client_id, request.view_id,
            workspace_ids, str(uuid4()), "web")
        session = _WebHostSession(identity)
        self._sessions[identity.generation] = session
        profile_id, extension_keys = request.profile_id, tuple(request.extension_keys)
        snapshot: ProfileExtensionInventory | None = None

        def freeze(inventory: ProfileExtensionInventory) -> None:
            nonlocal snapshot
            if self._admission_closed or session.closed or session.stopping.is_set():
                raise RuntimeError("Web 宿主启动期间 Backend 已撤销准入")
            members = {entry.extension_key: entry for entry in inventory.extensions}
            if (inventory.profile_id != profile_id or len(members) != len(inventory.extensions)
                or any(key not in members or not members[key].is_valid for key in extension_keys)):
                raise ValueError("Web 宿主请求不属于目标 profile 当前有效的精确安装版本")
            snapshot = ProfileExtensionInventory(profile_id=inventory.profile_id,
                extensions=[members[key] for key in extension_keys])
            # 原安装锁释放前发布到同一运行记录；后续更新不改变该实例的版本集合。
            session.profile_id = inventory.profile_id
            session.extension_keys = frozenset(extension_keys)

        try:
            await self.management.inventory(on_snapshot=freeze)
            if self._admission_closed or session.closed or session.stopping.is_set():
                raise RuntimeError("Web 宿主启动期间 Backend 已撤销准入")
            if snapshot is None:
                raise RuntimeError("扩展库存未在原安装锁内发布 Web 宿主快照")
            yield WebHostConnection(identity, snapshot, session.stopping, session.transport_settled)
        finally:
            session.stopping.set()
            await run_cleanup_shielded(lambda: self._release(session))

    async def close(self) -> None:
        self.close_admission()

        async def close_all() -> None:
            sessions = tuple(self._sessions.values())
            for session in sessions:
                session.stopping.set()
            results = await asyncio.gather(*(self._release(session) for session in sessions), return_exceptions=True)
            pending = [self._close_configuration()]
            if self._management is not None:
                pending.append(self._management.close())
            results.extend(await asyncio.gather(*pending, return_exceptions=True))
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise BaseExceptionGroup("Backend Node 宿主关闭未完成，保留原进程与输出归属", errors)

        await run_cleanup_shielded(close_all)

    async def _close_configuration(self) -> None:
        await self._configuration_watch.shutdown()
        self._configuration_closed = True
