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
from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.schemas.gateway.extension_host import (
    ExtensionHostFolderDTO,
    ExtensionHostInitializationDTO,
    ExtensionHostWorkspaceDTO,
)
from app.schemas.gateway.extension_installation import (
    ExtensionInstallationInstallDTO,
    ExtensionInstallationInstallRequest,
)
from app.services.infrastructure.extension_host.management import (
    NodeExtensionManagement,
)
from app.services.infrastructure.extension_host.process import NodeExtensionHost
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


@dataclass(frozen=True, slots=True)
class NodeHostIdentity:
    gateway_id: str
    backend_id: str
    client_id: str
    view_id: str
    workspace_ids: tuple[str, ...]
    generation: str


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
        self._sessions: dict[str, _HostSession] = {}
        self._admission_closed = False
        self._management: NodeExtensionManagement | None = None

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
        return not self._sessions and (self._management is None or self._management.drained)

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

    async def install(self, request: ExtensionInstallationInstallRequest) -> ExtensionInstallationInstallDTO:
        status = self.describe(expected_backend_id=request.backend_id, workspace_ids=tuple(request.workspace_ids))
        if status.reason is not None:
            raise ValueError(f"目标扩展安装不可用: {status.reason}")
        source = parse_resource_uri(request.source_uri, local_gateway_id=request.gateway_id)
        mount = self._mounts.resolve(source.scope_id)
        # 沿原文件 owner 解析及保护私有目录；不装配 Agent 容器或另建根绑定。
        paths = WorkspaceFilePaths(workspace=mount.workspace_root, workspace_id=mount.workspace_id, bindings=None)
        path = await run_sync_shielded(lambda: paths.native(source.display_uri))
        publication = await self.management.install(path)
        return ExtensionInstallationInstallDTO(**request.model_dump(exclude={"source_uri"}),
            installed=publication.installed, inventory=publication.inventory)

    async def _release(self, session: _HostSession) -> None:
        await session.close()
        self._sessions.pop(session.identity.generation, None)

    async def _initialization(self, identity: NodeHostIdentity, directory: Path) -> ExtensionHostInitializationDTO:
        environment = await self.management.environment()
        folders = []
        for workspace_id in identity.workspace_ids:
            mount = self._mounts.resolve(workspace_id)
            folders.append(ExtensionHostFolderDTO(workspace_id=workspace_id,
                resource_uri=resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files",
                    tail_segments=("root",), gateway_authority=identity.gateway_id),
                native_uri=mount.workspace_root.as_uri(), name=mount.workspace_root.name or str(mount.workspace_root)))
        # 安装域、明确视图和有序根集合决定 workspaceState 身份；客户端和临时 generation 不参与。
        view = json.dumps([identity.gateway_id, identity.backend_id, identity.view_id, identity.workspace_ids], separators=(",", ":"))
        workspace = ExtensionHostWorkspaceDTO(id=str(uuid5(NAMESPACE_URL, view)),
            name=", ".join(folder.name for folder in folders), folders=folders)
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
        if any((session.identity.gateway_id, session.identity.client_id, session.identity.view_id)
               == (gateway_id, client_id, view_id) for session in self._sessions.values()):
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

    async def close(self) -> None:
        self.close_admission()

        async def close_all() -> None:
            pending = [self._release(session) for session in tuple(self._sessions.values())]
            if self._management is not None:
                pending.append(self._management.close())
            results = await asyncio.gather(*pending, return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise BaseExceptionGroup("Backend Node 宿主关闭未完成，保留原进程与输出归属", errors)

        await run_cleanup_shielded(close_all)
