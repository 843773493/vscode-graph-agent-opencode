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
from typing import BinaryIO, Literal, overload
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.core.mounted_workspaces import MountedWorkspaceRegistry
from app.core.resource_uri import parse_resource_uri
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationProfileViewRequest,
    ExtensionConfigurationTargetRequest,
    ExtensionConfigurationViewDTO,
    ExtensionConfigurationViewRequest,
    ExtensionWebHostContextDTO,
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
    NativeExtensionFileRequest,
    ProfileExtensionInventory,
)
from app.schemas.gateway.extension_web import (
    ExtensionWebHostOpenRequest,
)
from app.schemas.gateway.mcp_definitions import (
    ExtensionMcpDefinitionChannelRequest,
    McpProviderSnapshotDTO,
)
from app.schemas.internal_v2.mcp import McpProviderDefinitionRefDTO
from app.services.infrastructure.config.source_uri import extension_configuration_uri
from app.services.infrastructure.extension_host.configuration import (
    ExtensionConfigurationFiles,
    initialize_configuration_files,
    workspace_configuration_file,
)
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementClient,
    NativeExtensionPublication,
    NativeFileRead,
    NativeFileStat,
    NativeManagementEnvironment,
    WebExtensionPublication,
)
from app.services.infrastructure.extension_host.mcp_definitions import (
    McpDefinitionChannel,
)
from app.services.infrastructure.extension_host.package_files import publication_file
from app.services.infrastructure.extension_host.process import NodeExtensionHost
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
from app.services.infrastructure.mcp.provider import (
    McpProviderAdmission,
    McpProviderUnavailable,
)
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
    context: ExtensionWebHostContextDTO
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
        self.mcp_definitions: McpDefinitionChannel | None = None
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
            if self.configuration is not None:
                await self.configuration.close()
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
        self.publications: dict[str, NativeExtensionPublication] = {}
        self.configuration: ExtensionConfigurationFiles | None = None
        self.mcp_definitions: McpDefinitionChannel | None = None
        self._lock = asyncio.Lock()
        self.pending_reads = 0
        self.reads_settled = asyncio.Event()
        self.reads_settled.set()
        self.closed = False
        self.stopping = asyncio.Event()
        self.transport_settled = asyncio.Event()
        self.transport_settled.set()

    async def close(self) -> None:
        self.stopping.set()
        await self.transport_settled.wait()
        await self.reads_settled.wait()
        async with self._lock:
            if self.configuration is not None:
                await self.configuration.close()
            self.closed = True


class NodeExtensionHostOwner:
    """一个 Backend 的惰性宿主集合；内部准入只接受入口已认证的客户端身份。"""

    # TODO(5.4/5.6)：迁齐MainThread初始载荷、事件的工作区投影与窗口贡献代际；
    # 已认证通道和内部集合校验不能代替RPC载荷的视图隔离。

    def __init__(self, *, backend_id: str, mounted_workspaces: MountedWorkspaceRegistry,
                 runtime: NodeHostRuntime | None, home: Path, environment: Mapping[str, str],
                 startup_timeout: float, management: ExtensionManagementClient | None) -> None:
        self.backend_id = backend_id
        self.runtime = runtime
        self._mounts = mounted_workspaces
        self._home = home
        self._environment = dict(environment)
        self._startup_timeout = startup_timeout
        self._sessions: dict[str, _HostSession | _WebHostSession] = {}
        self._admission_closed = False
        self._management = management
        self._configuration_watch = WorkspaceFileWatchService(workspace_root=home / "state/backend/extension-management" / backend_id)
        self._configuration_closed = True
        self._configuration_lock = asyncio.Lock()
        self._configuration_stopping = asyncio.Event()
        self._configuration_environment: NativeManagementEnvironment | None = None

    @property
    def active(self) -> tuple[NodeHostIdentity, ...]:
        return tuple(session.identity for session in self._sessions.values())

    def close_admission(self) -> None:
        self._admission_closed = True
        self._configuration_stopping.set()
        for session in self._sessions.values():
            if session.mcp_definitions is not None:
                session.mcp_definitions.close()
        if self._management is not None:
            self._management.close_admission()

    @property
    def management(self) -> ExtensionManagementClient:
        if self._admission_closed:
            raise RuntimeError("Backend 正在关闭，拒绝扩展管理")
        if self._management is None:
            raise NotImplementedError("Backend 未连接已配置的原生 Gateway 扩展管理服务")
        return self._management

    @property
    def drained(self) -> bool:
        return not self._sessions and self._configuration_closed and (self._management is None or self._management.drained)

    def _check_configuration_target(self, request: ExtensionConfigurationTargetRequest, client_id: str) -> None:
        self.management_target(request.backend_id, require_management=True)
        if not client_id.strip():
            raise ValueError("配置请求必须绑定已认证客户端")
        for workspace_id in request.workspace_ids:
            self._mounts.resolve(workspace_id)

    async def _native_configuration_environment(self) -> NativeManagementEnvironment:
        # 原安装 owner 的 profile 描述在本客户端生命周期内不变；文件操作不反复请求 Node。
        async with self._configuration_lock:
            if self._configuration_environment is None:
                self._configuration_environment = await self.management.environment()
            return self._configuration_environment

    async def prepare_configuration(self, request: ExtensionConfigurationTargetRequest, *, client_id: str) -> ExtensionConfigurationViewDTO:
        self._check_configuration_target(request, client_id)
        environment = await self._native_configuration_environment()
        async with self._configuration_lock:
            self._check_configuration_target(request, client_id)
            self._configuration_closed = False
            context = await self._configuration_context(request, environment, initialize=True)
            self._check_configuration_target(request, client_id)
        return ExtensionConfigurationViewDTO(**request.model_dump(exclude={"gateway_connection_id"}),
            profile_id=environment.profile_id, context=context)

    async def configuration_files(self, request: ExtensionConfigurationViewRequest | ExtensionConfigurationProfileViewRequest, *, client_id: str) -> ExtensionConfigurationFiles:
        if isinstance(request, ExtensionConfigurationProfileViewRequest):
            self._check_configuration_target(request, client_id)
            environment = await self._native_configuration_environment()
            if request.profile_id != environment.profile_id:
                raise ValueError("配置请求不属于目标安装域的明确 profile")
            context = await self._configuration_context(request, environment, initialize=False)
            self._check_configuration_target(request, client_id)
            self._configuration_closed = False
            return ExtensionConfigurationFiles(context.remote_settings, context.workspace, self._configuration_watch,
                self._configuration_stopping, lock=self._configuration_lock)
        session = self._sessions.get(request.generation)
        if (self._admission_closed or session is None or session.closed or session.stopping.is_set()
            or session.identity != NodeHostIdentity(request.gateway_id, request.backend_id, client_id,
                request.view_id, tuple(request.workspace_ids), request.generation, session.identity.host_kind)
            or session.configuration is None):
            raise ValueError("配置请求不属于当前已认证的宿主代际和明确工作区视图")
        return session.configuration

    def _mcp_session(self, request: ExtensionMcpDefinitionChannelRequest, *, client_id: str) -> _HostSession | _WebHostSession:
        session = self._sessions.get(request.generation)
        if (self._admission_closed or session is None or session.closed or session.stopping.is_set()
            or session.identity != NodeHostIdentity(request.gateway_id, request.backend_id, client_id,
                request.view_id, tuple(request.workspace_ids), request.generation, request.host_kind)):
            raise McpProviderUnavailable("Provider 通道不属于原已认证宿主代际和工作区视图")
        return session

    @asynccontextmanager
    async def mcp_definition_channel(self, request: ExtensionMcpDefinitionChannelRequest, *, client_id: str) -> AsyncIterator[tuple[McpDefinitionChannel, asyncio.Event]]:
        session = self._mcp_session(request, client_id=client_id)
        if session.mcp_definitions is not None:
            raise ValueError("该宿主代际已有 Provider 控制通道，不能覆盖")
        channel = McpDefinitionChannel()
        session.mcp_definitions = channel
        try:
            yield channel, session.stopping
        finally:
            channel.close()
            session.mcp_definitions = None

    async def validate_mcp_snapshot(self, request: ExtensionMcpDefinitionChannelRequest, snapshot: McpProviderSnapshotDTO, *, client_id: str) -> None:
        session = self._mcp_session(request, client_id=client_id)
        inventory = await self.management.inventory()
        self._mcp_session(request, client_id=client_id)
        entry = next((item for item in inventory.extensions if item.extension_key == snapshot.extension_key and item.is_valid), None)
        description = entry.description if entry is not None else None
        if not isinstance(description, dict):
            raise McpProviderUnavailable("Provider 原 VSIX 安装实例已不可用")
        identifier = description.get("identifier")
        contributes = description.get("contributes")
        providers = contributes.get("mcpServerDefinitionProviders") if isinstance(contributes, dict) else None
        extension_id = identifier.get("value") if isinstance(identifier, dict) else None
        if (not isinstance(extension_id, str) or extension_id.lower() != snapshot.extension_id.lower()
            or not isinstance(providers, list) or not any(isinstance(item, dict)
                and f"{extension_id.lower()}/{item.get('id')}" == snapshot.collection_id for item in providers)
            or isinstance(session, _WebHostSession) and snapshot.extension_key not in session.publications):
            raise McpProviderUnavailable("Provider 不属于原 VSIX 的声明与宿主安装实例")

    def check_mcp_definition(self, reference: McpProviderDefinitionRefDTO, *, workspace_id: str,
                             transport: Literal["stdio", "streamable_http"]) -> None:
        session = self._sessions.get(reference.host_generation)
        if (self._admission_closed or session is None or session.closed or session.stopping.is_set()
            or (session.identity.gateway_id, session.identity.backend_id, session.identity.client_id,
                session.identity.view_id, session.identity.host_kind) != (reference.gateway_id, reference.backend_id,
                reference.client_id, reference.view_id, reference.host_kind)
            or workspace_id not in session.identity.workspace_ids or session.mcp_definitions is None):
            raise McpProviderUnavailable("原 Provider 宿主离线、身份已撤销或工作区不属于原目标")
        snapshot = session.mcp_definitions.definition(reference.provider_ref, reference.definition_ref).value
        definition = next(item for item in snapshot.definitions if item.definition_ref == reference.definition_ref)
        if ((snapshot.extension_id, snapshot.extension_key, snapshot.collection_id) != (reference.extension_id,
                reference.extension_key, reference.collection_id) or definition.transport != transport
            or reference.host_kind == "web" and transport == "stdio"):
            raise McpProviderUnavailable("Provider 原定义、安装实例或执行上下文不匹配")

    async def resolve_mcp_definition(self, reference: McpProviderDefinitionRefDTO, *, workspace_id: str,
                                     transport: Literal["stdio", "streamable_http"]) -> McpProviderAdmission:
        self.check_mcp_definition(reference, workspace_id=workspace_id, transport=transport)
        session = self._sessions[reference.host_generation]
        channel = session.mcp_definitions
        assert channel is not None
        snapshot = channel.definition(reference.provider_ref, reference.definition_ref)
        request = ExtensionMcpDefinitionChannelRequest(protocol_version=1, gateway_id=reference.gateway_id,
            backend_id=reference.backend_id, view_id=reference.view_id, generation=reference.host_generation,
            workspace_ids=list(session.identity.workspace_ids), host_kind=reference.host_kind)

        def check() -> None:
            self.check_mcp_definition(reference, workspace_id=workspace_id, transport=transport)
            if snapshot.invalidated.is_set() or channel.definition(reference.provider_ref, reference.definition_ref) is not snapshot:
                raise McpProviderUnavailable("Provider 原快照已变化，拒绝迟到启动")

        await self.validate_mcp_snapshot(request, snapshot.value, client_id=reference.client_id)
        check()
        admission = await channel.resolve(reference.provider_ref, reference.definition_ref, check)
        await self.validate_mcp_snapshot(request, snapshot.value, client_id=reference.client_id)
        check()
        if admission.launch.transport != transport:
            raise McpProviderUnavailable("Provider resolve 改变了原定义的传输类型")
        return admission

    def check_web_extension_file(self, request: NativeExtensionFileRequest, *, client_id: str) -> tuple[_WebHostSession, NativeExtensionPublication]:
        session = self._sessions.get(request.generation or "")
        if (self._admission_closed or not isinstance(session, _WebHostSession)
            or session.closed or session.stopping.is_set() or request.current_profile_member
            or session.identity != NodeHostIdentity(request.gateway_id, request.backend_id, client_id,
                request.view_id or "", session.identity.workspace_ids, request.generation or "", "web")
            or request.profile_id != session.profile_id):
            raise ValueError("扩展包请求不属于当前已认证的 Web 宿主代际和冻结版本")
        publication = session.publications.get(request.extension_key)
        if publication is None or request.extension_id.lower() != publication.extension_id.lower():
            raise ValueError("扩展包请求不属于当前已认证的 Web 宿主代际和冻结版本")
        return session, publication

    @overload
    async def web_extension_file(self, request: NativeExtensionFileRequest, *, client_id: str,
                                 operation: Literal["stat"]) -> NativeFileStat: ...

    @overload
    async def web_extension_file(self, request: NativeExtensionFileRequest, *, client_id: str,
                                 operation: Literal["read"]) -> NativeFileRead: ...

    async def web_extension_file(self, request: NativeExtensionFileRequest, *, client_id: str,
                                 operation: Literal["stat", "read"]) -> NativeFileStat | NativeFileRead:
        session, publication = self.check_web_extension_file(request, client_id=client_id)
        # 准入与占用登记之间没有 await；关闭先撤准入，再等待原同步 worker 真正结清。
        session.pending_reads += 1
        session.reads_settled.clear()
        try:
            result = await run_sync_shielded(publication_file, publication, extension_key=request.extension_key,
                relative_segments=request.relative_segments, file_operation=operation)
            self.check_web_extension_file(request, client_id=client_id)
            return result
        finally:
            session.pending_reads -= 1
            if not session.pending_reads:
                session.reads_settled.set()

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

    def management_target(self, expected_backend_id: str | None = None, *, require_management: bool = False) -> str:
        if expected_backend_id is not None and expected_backend_id != self.backend_id:
            raise ValueError("扩展安装域 Backend 身份不匹配")
        if self._admission_closed:
            raise ValueError("Backend 正在关闭，拒绝扩展管理")
        if require_management and self._management is None:
            raise ValueError("Backend 未连接已配置的原生 Gateway 扩展管理服务")
        return self.backend_id

    async def install(self, request: ExtensionInstallationInstallRequest) -> ExtensionInstallationInstallDTO:
        self.management_target(request.backend_id, require_management=True)
        for workspace_id in request.workspace_ids:
            self._mounts.resolve(workspace_id)
        source = parse_resource_uri(request.source_uri, local_gateway_id=request.gateway_id)
        mount = self._mounts.resolve(source.scope_id)
        # 文件 owner 在 Backend 目标机解析并保护路径；Gateway 只用此原生参数调用同机安装域。
        paths = WorkspaceFilePaths(workspace=mount.workspace_root, workspace_id=mount.workspace_id, bindings=None)
        vsix_path = await run_sync_shielded(lambda: paths.native(source.display_uri))
        publication = await self.management.install(vsix_path)
        return ExtensionInstallationInstallDTO(**request.model_dump(exclude={"source_uri", "gateway_connection_id"}),
            installed=publication.installed, inventory=publication.inventory)

    async def _release(self, session: _HostSession | _WebHostSession) -> None:
        if session.mcp_definitions is not None:
            session.mcp_definitions.close()
        await session.close()
        self._sessions.pop(session.identity.generation, None)

    async def _initialization(self, identity: NodeHostIdentity, directory: Path) -> ExtensionHostInitializationDTO:
        native_environment = await self._native_configuration_environment()
        context = await self._configuration_context(identity, native_environment, initialize=True)
        environment = ExtensionHostEnvironmentDTO(**native_environment.model_dump(exclude={"settings_native_uri", "untitled_workspaces_home_uri"}),
            remote_settings=context.remote_settings)
        return ExtensionHostInitializationDTO(os=context.os,
            parent_pid=os.getpid(), environment=environment, logs_location_uri=directory.as_uri(), workspace=context.workspace)

    async def _configuration_context(self, identity: NodeHostIdentity | ExtensionConfigurationTargetRequest, environment: NativeManagementEnvironment,
                                     *, initialize: bool) -> ExtensionWebHostContextDTO:
        settings = ExtensionHostConfigurationResourceDTO(native_uri=environment.settings_native_uri,
            resource_uri=extension_configuration_uri(gateway_id=identity.gateway_id, backend_id=identity.backend_id,
                                                    profile_id=environment.profile_id))
        workspace = await self._workspace(identity, environment, initialize=initialize)
        return ExtensionWebHostContextDTO(os="windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux",
            remote_settings=settings, workspace=workspace)

    async def _workspace(self, identity: NodeHostIdentity | ExtensionConfigurationTargetRequest, native_environment: NativeManagementEnvironment,
                         *, initialize: bool = True) -> ExtensionHostWorkspaceDTO:
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
            name=", ".join(folder.name for folder in folders) or "空工作区", folders=folders)
        if initialize:
            location = await run_sync_shielded(initialize_configuration_files, native_environment.settings_native_uri,
                native_environment.untitled_workspaces_home_uri, workspace)
        else:
            path = workspace_configuration_file(native_environment.untitled_workspaces_home_uri, workspace)
            location = path.as_uri() if path is not None else None
        if location is not None:
            workspace = workspace.model_copy(update={"configuration": ExtensionHostConfigurationResourceDTO(
                native_uri=location, resource_uri=extension_configuration_uri(gateway_id=identity.gateway_id,
                    backend_id=identity.backend_id, workspace_id=workspace.id))})
        return workspace

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
        _ = self.management
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
            # 初始化文件 worker 与原 session 关闭共用准入锁，关闭不能越过未结算的首次发布。
            async with session._lock:
                if session.stopping.is_set():
                    raise RuntimeError("Node 宿主初始化期间 Backend 已撤销准入")
                initialization = await self._initialization(identity, directory)
                self._configuration_closed = False
                session.configuration = ExtensionConfigurationFiles(initialization.environment.remote_settings, initialization.workspace,
                    self._configuration_watch, session.stopping)
            if self._admission_closed:
                raise RuntimeError("Node 宿主初始化期间 Backend 已撤销准入")
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
        self.management_target(request.backend_id, require_management=True)
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

        def freeze(publication: WebExtensionPublication) -> None:
            nonlocal snapshot
            if self._admission_closed or session.closed or session.stopping.is_set():
                raise RuntimeError("Web 宿主启动期间 Backend 已撤销准入")
            inventory = publication.inventory
            members = {entry.extension_key: entry for entry in inventory.extensions}
            publications = {entry.extension_key: entry for entry in publication.publications}
            if (inventory.profile_id != profile_id or publication.environment.profile_id != profile_id
                or len(members) != len(inventory.extensions)
                or len(publications) != len(publication.publications) or set(publications) != set(extension_keys)
                or any(key not in members or not members[key].is_valid for key in extension_keys)):
                raise ValueError("Web 宿主请求不属于目标 profile 当前有效的精确安装版本")
            snapshot = ProfileExtensionInventory(profile_id=inventory.profile_id,
                target_platform=inventory.target_platform, extensions=[members[key] for key in extension_keys])
            # 冻结同次原 scanner 发布描述；后续文件 I/O 不再逐文件调用 Gateway／Node。
            # TODO(4.4)：Node 实际运行集合及最终引用回收未闭合，当前继续禁止全局物理 cleanUp。
            session.profile_id = inventory.profile_id
            session.publications = publications

        try:
            publication = await self.management.web_publication(profile_id=profile_id, extension_keys=list(extension_keys))
            freeze(publication)
            if self._admission_closed or session.closed or session.stopping.is_set():
                raise RuntimeError("Web 宿主启动期间 Backend 已撤销准入")
            if snapshot is None:
                raise RuntimeError("原扩展库存回执未生成 Web 宿主快照")
            # 与 Node 共用目标文件 owner；Web 仅取得业务视图，不伪造进程初始化。
            async with session._lock:
                if self._admission_closed or session.stopping.is_set():
                    raise RuntimeError("Web 宿主初始化期间 Backend 已撤销准入")
                context = await self._configuration_context(identity, publication.environment, initialize=True)
                if self._admission_closed or session.stopping.is_set():
                    raise RuntimeError("Web 宿主初始化期间 Backend 已撤销准入")
                self._configuration_closed = False
                session.configuration = ExtensionConfigurationFiles(context.remote_settings, context.workspace,
                    self._configuration_watch, session.stopping)
            yield WebHostConnection(identity, snapshot, context, session.stopping, session.transport_settled)
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
        async with self._configuration_lock:
            await self._configuration_watch.shutdown()
            self._configuration_closed = True
