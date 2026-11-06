"""Backend 独占宿主进程及关闭责任；工作区根只解析原 mounted registry。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.core.mounted_workspaces import MountedWorkspaceRegistry
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.services.infrastructure.extension_host.process import NodeExtensionHost
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime


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
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    host: NodeExtensionHost
    stopping: asyncio.Event
    transport_settled: asyncio.Event


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

    async def open(self) -> NodeHostConnection:
        async with self._lock:
            if self.closed:
                raise RuntimeError("Node 宿主 generation 已撤销，不能重新连接")
            self._directory.mkdir(parents=True, exist_ok=False)
            self._output = (self._directory / "host.log").open("xb")
            self.host = NodeExtensionHost(self.identity.generation, runtime=self._runtime,
                directory=self._directory, environment=self._environment,
                output=self._output, startup_timeout=self._startup_timeout)
            reader, writer = await self._scope.enter_async_context(self.host.connect())
            return NodeHostConnection(self.identity, reader, writer, self.host, self.stopping, self.transport_settled)

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

    @property
    def active(self) -> tuple[NodeHostIdentity, ...]:
        return tuple(session.identity for session in self._sessions.values())

    def close_admission(self) -> None:
        self._admission_closed = True

    async def _release(self, session: _HostSession) -> None:
        await session.close()
        self._sessions.pop(session.identity.generation, None)

    @asynccontextmanager
    async def connect(self, *, gateway_id: str, expected_backend_id: str, client_id: str,
                      view_id: str, workspace_ids: tuple[str, ...]) -> AsyncIterator[NodeHostConnection]:
        if self._admission_closed:
            raise RuntimeError("Backend 正在关闭，拒绝新的 Node 宿主")
        if expected_backend_id != self.backend_id:
            raise ValueError("Node 宿主目标 Backend 身份不匹配")
        if not all(value.strip() for value in (gateway_id, client_id, view_id)):
            raise ValueError("Node 宿主必须绑定已认证客户端和明确工作区视图")
        workspace_ids = tuple(workspace_ids)
        if not workspace_ids or len(set(workspace_ids)) != len(workspace_ids):
            raise ValueError("Node 宿主工作区视图必须非空且不能重复")
        for workspace_id in workspace_ids:
            self._mounts.resolve(workspace_id)
        if self.runtime is None:
            raise NotImplementedError("目标未提供专用 Node Extension Host 运行时")
        # TODO: Windows named pipe / macOS 私有IPC验收后启用，不能落到其它目标。
        if sys.platform != "linux":
            raise NotImplementedError("Node Extension Host 当前仅验证 Linux 目标")
        if any((session.identity.gateway_id, session.identity.client_id, session.identity.view_id)
               == (gateway_id, client_id, view_id) for session in self._sessions.values()):
            raise ValueError("该客户端的 Node 宿主视图尚未关闭；不能覆盖原 generation")
        identity = NodeHostIdentity(gateway_id, self.backend_id, client_id, view_id, workspace_ids, str(uuid4()))
        session = _HostSession(identity, runtime=self.runtime,
            directory=self._home / "state/backend/extension-hosts" / identity.generation,
            environment=self._environment, startup_timeout=self._startup_timeout)
        # 预留与关闭快照之间没有await；启动中的实例同样归原owner回收。
        self._sessions[identity.generation] = session
        failure: BaseException | None = None
        try:
            connection = await session.open()
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
            results = await asyncio.gather(*(self._release(session) for session in tuple(self._sessions.values())),
                                           return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise BaseExceptionGroup("Backend Node 宿主关闭未完成，保留原进程与输出归属", errors)

        await run_cleanup_shielded(close_all)
