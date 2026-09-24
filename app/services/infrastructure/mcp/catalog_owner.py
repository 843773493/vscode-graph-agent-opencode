"""MCP 工具目录唯一 owner（OpenSpec C8-B / M01 基础）。

职责边界：

- 连接生命周期：经 ``create_session`` + ``session_kwargs.message_handler`` 建立
  各 enabled server 的唯一 session（``MultiServerMCPClient.session()`` 不暴露
  message handler，而底层是同一个 ``create_session`` 工厂，不产生第二套连接实现）。
- 目录 relist：``load_mcp_tools`` 是唯一的工具发现/适配路径，内部经
  ``_list_all_tools`` 完整分页读取 ``tools/list``。
- 不可变目录 revision：对「server -> 有序工具描述 + 参数指纹」的语义载荷做
  canonical JSON sha256；payload 不变则 revision 不推进。
- 变化通知：消费 ``notifications/tools/list_changed``；不具备通知能力的 server
  只在显式激活边界经 ``relist_servers_without_notifications`` 主动 relist。
- generation lease：每个连接代际持有单调 generation；旧连接回调与迟到的 relist
  结果一律丢弃，绝不发布。
- 事件：目录变化发布轻量事件到独立 ``mcp.catalog/workspace`` channel，只携带
  identity/revision；不进入 job.events，也不注册 ResourceRegistry source/CSM。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypeAlias

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import Connection, create_session
from langchain_mcp_adapters.tools import load_mcp_tools

from app.services.infrastructure.events.channel_events import McpCatalogEventPublisher
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp.config import (
    McpServerConfig,
    parse_mcp_server_configs,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingRef,
    ExtensionTargetBindingInput,
    build_extension_catalog_binding,
    extension_args_fingerprint,
    payload_digest,
)
from app.services.infrastructure.mcp.naming import build_mcp_tool_id
from mcp import types as mcp_types
from mcp.client.session import ClientSession

logger = logging.getLogger(__name__)

McpChangeNotifyCallback: TypeAlias = Callable[[], None]
McpSessionFactory: TypeAlias = Callable[
    [McpServerConfig, McpChangeNotifyCallback],
    AbstractAsyncContextManager["McpServerSessionPort"],
]

_MAX_ERROR_TEXT_LENGTH = 300


class McpCatalogError(RuntimeError):
    """MCP 目录 owner 的显式错误基类。"""


class McpCatalogRelistError(McpCatalogError):
    """显式 relist 失败；旧 valid revision 保留，不做半发布。"""


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    tool_id: str
    server_id: str
    remote_name: str
    description: str


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    server_id: str
    transport: str
    enabled: bool
    status: str
    tools: tuple[McpToolDescriptor, ...]
    # 最近一次 relist 的有界错误摘要；成功 relist 后清空。None 表示无失败记录。
    last_relist_error: str | None = None


@dataclass(frozen=True, slots=True)
class McpCatalogSnapshot:
    """不可变目录快照：一个 revision 与它对应的全部工具对象。

    server 连接状态是运行时视图，由 :meth:`McpCatalogOwner.list_servers` 现读，
    不进入不可变目录语义。
    """

    revision: str
    tools: Mapping[str, BaseTool]


class McpServerSessionPort(Protocol):
    """owner 与具体 MCP client 实现之间的窄端口；测试注入替身。"""

    async def initialize(self) -> None: ...

    async def list_tools(self) -> list[BaseTool]: ...

    def supports_tool_list_changed(self) -> bool: ...


class _LangchainServerSession:
    """``mcp`` ClientSession 的窄端口包装。"""

    def __init__(self, session: ClientSession, *, server_name: str) -> None:
        self._session = session
        self._server_name = server_name

    async def initialize(self) -> None:
        await self._session.initialize()

    async def list_tools(self) -> list[BaseTool]:
        # load_mcp_tools 内部经 _list_all_tools 完整分页读取 tools/list。
        return await load_mcp_tools(
            self._session,
            server_name=self._server_name,
            handle_tool_errors=True,
        )

    def supports_tool_list_changed(self) -> bool:
        capabilities = self._session.get_server_capabilities()
        return bool(
            capabilities is not None
            and capabilities.tools is not None
            and capabilities.tools.listChanged
        )


def production_mcp_session_factory(
    server: McpServerConfig,
    on_tools_list_changed: McpChangeNotifyCallback,
) -> AbstractAsyncContextManager[McpServerSessionPort]:
    """生产 session 工厂：``create_session`` + ``session_kwargs.message_handler``。

    ``MultiServerMCPClient.session()`` 不暴露 message handler，因此直接使用
    同一个底层 ``create_session`` 工厂，把 ``tools/list_changed`` 通知消费接入
    ``session_kwargs``；工具发现仍只经 ``load_mcp_tools``。
    """

    async def _message_handler(message: object) -> None:
        if isinstance(message, mcp_types.ServerNotification) and isinstance(
            message.root, mcp_types.ToolListChangedNotification
        ):
            on_tools_list_changed()

    # Connection 是 TypedDict；create_session 会把 session_kwargs 透传给 ClientSession。
    connection: Connection = {
        **server.connection,
        "session_kwargs": {"message_handler": _message_handler},
    }

    @asynccontextmanager
    async def _session_context() -> AsyncIterator[McpServerSessionPort]:
        async with create_session(connection) as session:
            port = _LangchainServerSession(session, server_name=server.server_id)
            await port.initialize()
            yield port

    return _session_context()


def _bounded_error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:_MAX_ERROR_TEXT_LENGTH]


def _compute_catalog_revision(
    tools_by_id: Mapping[str, BaseTool],
    descriptors_by_server: Mapping[str, tuple[McpToolDescriptor, ...]],
) -> str:
    """语义目录载荷的 sha256 revision（canonical JSON；与事件合同同形状）。"""
    payload: list[list[object]] = []
    for server_id in sorted(descriptors_by_server):
        server_entries: list[list[str]] = []
        for descriptor in sorted(
            descriptors_by_server[server_id], key=lambda item: item.tool_id
        ):
            tool = tools_by_id[descriptor.tool_id]
            server_entries.append(
                [
                    descriptor.tool_id,
                    descriptor.remote_name,
                    descriptor.description,
                    extension_args_fingerprint(tool.args),
                ]
            )
        payload.append([server_id, server_entries])
    return payload_digest(
        payload,
        context="MCP 目录语义载荷",
        error_type=McpCatalogError,
    )


def _adapt_server_tools(
    server_id: str,
    remote_tools: list[BaseTool],
    *,
    foreign_tool_ids: set[str],
) -> tuple[dict[str, BaseTool], tuple[McpToolDescriptor, ...]]:
    """把远端工具适配为带 server 命名空间的 LangChain 工具与目录描述。"""
    tools: dict[str, BaseTool] = {}
    descriptors: list[McpToolDescriptor] = []
    for remote_tool in remote_tools:
        tool_id = build_mcp_tool_id(server_id, remote_tool.name)
        if tool_id in foreign_tool_ids or tool_id in tools:
            raise ValueError(
                "MCP 工具命名冲突，禁止静默覆盖: "
                f"server_id={server_id} remote_name={remote_tool.name} "
                f"tool_id={tool_id}"
            )
        metadata = {
            **dict(remote_tool.metadata or {}),
            "mcp_server_id": server_id,
            "mcp_remote_tool_name": remote_tool.name,
        }
        tools[tool_id] = remote_tool.model_copy(
            update={"name": tool_id, "metadata": metadata}
        )
        descriptors.append(
            McpToolDescriptor(
                tool_id=tool_id,
                server_id=server_id,
                remote_name=remote_tool.name,
                description=remote_tool.description or "",
            )
        )
    return tools, tuple(descriptors)


@dataclass(slots=True)
class _ServerRuntime:
    """一个 enabled server 的连接运行时：session、generation lease 与错误记录。"""

    server: McpServerConfig
    generation: int
    session: McpServerSessionPort
    last_relist_error: str | None = None


class McpCatalogOwner:
    """MCP 工具目录唯一 owner。

    启动/通知/显式 relist 都收敛到同一条发布链路；目录为空也发布固定
    envelope/revision；连续 relist payload 不变时只发 ``unchanged``，不推进
    revision；增删改发布新 immutable revision，删除以 ``tombstone`` 标记。
    """

    def __init__(
        self,
        *,
        raw_config: object,
        workspace_root: Path,
        event_service: EventChannelService,
        session_factory: McpSessionFactory | None = None,
    ) -> None:
        self._servers = parse_mcp_server_configs(
            raw_config,
            workspace_root=workspace_root,
        )
        self._publisher = McpCatalogEventPublisher(event_service=event_service)
        self._session_factory: McpSessionFactory = (
            session_factory or production_mcp_session_factory
        )
        self._session_stack: AsyncExitStack | None = None
        self._started = False
        self._snapshot: McpCatalogSnapshot | None = None
        self._tools_by_id: dict[str, BaseTool] = {}
        self._descriptors_by_server: dict[str, tuple[McpToolDescriptor, ...]] = {}
        # 目录 generation lease：revision 每实际推进一次单调 +1；unchanged 不推进。
        self._catalog_generation = 0
        self._runtimes: dict[str, _ServerRuntime] = {}
        self._catalog_lock = asyncio.Lock()
        self._pending_relist_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("McpCatalogOwner 不允许重复启动")
        session_stack = AsyncExitStack()
        await session_stack.__aenter__()
        try:
            for server in self._servers:
                if not server.enabled:
                    continue
                runtime = _ServerRuntime(
                    server=server,
                    generation=1,
                    session=await session_stack.enter_async_context(
                        self._session_factory(
                            server,
                            self._make_tools_list_changed_callback(
                                server.server_id, 1
                            ),
                        )
                    ),
                )
                self._runtimes[server.server_id] = runtime
                await self._apply_relist(runtime, source="startup")
            if self._snapshot is None:
                # 目录为空（零 server / 全 disabled）也发布固定 envelope/revision。
                async with self._catalog_lock:
                    if self._snapshot is None:
                        self._publish_snapshot_locked(
                            server_id=None,
                            candidate_tools={},
                            candidate_descriptors={},
                        )
        except Exception as error:
            await session_stack.aclose()
            self._runtimes.clear()
            raise McpCatalogError(
                f"MCP Server 启动或工具目录发现失败: {error}"
            ) from error
        self._session_stack = session_stack
        self._started = True

    async def shutdown(self) -> None:
        for task in tuple(self._pending_relist_tasks):
            task.cancel()
        if self._pending_relist_tasks:
            await asyncio.gather(
                *tuple(self._pending_relist_tasks), return_exceptions=True
            )
        if self._session_stack is not None:
            await self._session_stack.aclose()
            self._session_stack = None
        self._runtimes.clear()
        self._tools_by_id.clear()
        self._descriptors_by_server.clear()
        self._snapshot = None
        self._started = False

    # ------------------------------------------------------------------
    # 消费面（Agent / API / 健康检查）
    # ------------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    @property
    def catalog_channel_name(self) -> str:
        return self._publisher.channel_name

    def get_tools(self) -> list[BaseTool]:
        self._require_started()
        assert self._snapshot is not None
        return list(self._snapshot.tools.values())

    def get_tool_ids(self) -> frozenset[str]:
        self._require_started()
        assert self._snapshot is not None
        return frozenset(self._snapshot.tools)

    def list_servers(self) -> list[McpServerSnapshot]:
        self._require_started()
        return list(self._build_server_snapshots())

    def catalog_revision(self) -> str:
        self._require_started()
        assert self._snapshot is not None
        return self._snapshot.revision

    def catalog_generation(self) -> int:
        """当前目录 generation lease；revision 推进时单调递增。"""
        self._require_started()
        return self._catalog_generation

    def binding_snapshot(self) -> ExtensionCatalogBindingRef:
        """当前目录的 sealed binding ref；目录后续变化不影响已返回的 ref。

        发布链路只在持有 _catalog_lock 时改写目录字段；本方法在同一事件循
        环线程内同步读取并完成冻结构造，读取与构造之间无 await，不存在撕
        裂窗口。schema_hash 冻结自当前工具参数 schema，与语义 revision 同源。
        """
        self._require_started()
        assert self._snapshot is not None
        targets = [
            ExtensionTargetBindingInput(
                target_id=descriptor.tool_id,
                origin="mcp",
                server_id=descriptor.server_id,
                args=self._tools_by_id[descriptor.tool_id].args,
            )
            for server_descriptors in self._descriptors_by_server.values()
            for descriptor in server_descriptors
        ]
        return build_extension_catalog_binding(
            catalog_revision=self._snapshot.revision,
            generation=self._catalog_generation,
            targets=targets,
        )

    def tool_descriptors(self) -> tuple[McpToolDescriptor, ...]:
        """全部目录 descriptor（按 tool_id 排序）；供受信派生方消费。"""
        self._require_started()
        return tuple(
            sorted(
                (
                    descriptor
                    for descriptors in self._descriptors_by_server.values()
                    for descriptor in descriptors
                ),
                key=lambda item: item.tool_id,
            )
        )

    def servers_without_change_notifications(self) -> tuple[str, ...]:
        """当前不具备 ``tools/list_changed`` 通知能力的已连接 server。"""
        self._require_started()
        return tuple(
            sorted(
                server_id
                for server_id, runtime in self._runtimes.items()
                if not runtime.session.supports_tool_list_changed()
            )
        )

    async def relist_servers_without_notifications(self) -> None:
        """激活边界（安全点）主动 relist 无通知 server；失败显式抛出。

        E4 的 activation coordinator 在每个允许的激活边界调用本方法；本方法
        不做网络节流，也不把失败伪装成成功。
        """
        self._require_started()
        for server_id in self.servers_without_change_notifications():
            await self._apply_relist(self._runtimes[server_id], source="activation")

    # ------------------------------------------------------------------
    # 通知与 relist
    # ------------------------------------------------------------------

    def _make_tools_list_changed_callback(
        self,
        server_id: str,
        generation: int,
    ) -> McpChangeNotifyCallback:
        def _on_notification() -> None:
            self._handle_tools_list_changed(server_id, generation)

        return _on_notification

    def _handle_tools_list_changed(self, server_id: str, generation: int) -> None:
        """通知回调入口：generation lease 校验后调度 relist 任务。"""
        if not self._started:
            return
        runtime = self._runtimes.get(server_id)
        if runtime is None or runtime.generation != generation:
            logger.info(
                "忽略过期 MCP 连接的 tools/list_changed 回调: "
                "server_id=%s callback_generation=%d current_generation=%s",
                server_id,
                generation,
                runtime.generation if runtime is not None else None,
            )
            return
        task = asyncio.create_task(
            self._apply_relist(runtime, source="notification")
        )
        self._pending_relist_tasks.add(task)
        task.add_done_callback(self._pending_relist_tasks.discard)

    async def _apply_relist(self, runtime: _ServerRuntime, *, source: str) -> None:
        """完整 relist 一个 server 并原子发布；失败保留旧 valid revision。"""
        server_id = runtime.server.server_id
        generation_at_start = runtime.generation
        try:
            remote_tools = await runtime.session.list_tools()
        except Exception as error:
            runtime.last_relist_error = _bounded_error_text(error)
            if source == "notification":
                logger.exception(
                    "MCP relist 失败，保留旧目录 revision: server_id=%s", server_id
                )
                return
            raise McpCatalogRelistError(
                f"MCP 工具目录 relist 失败: server_id={server_id}: {error}"
            ) from error
        async with self._catalog_lock:
            # 等待锁期间连接可能已被重建/关闭：二次 generation lease 校验。
            if (
                runtime.generation != generation_at_start
                or self._runtimes.get(server_id) is not runtime
            ):
                logger.info(
                    "MCP relist 结果所属连接代际已失效，丢弃: server_id=%s",
                    server_id,
                )
                return
            runtime.last_relist_error = None
            own_ids = {
                descriptor.tool_id
                for descriptor in self._descriptors_by_server.get(server_id, ())
            }
            foreign_tool_ids = set(self._tools_by_id) - own_ids
            server_tools, descriptors = _adapt_server_tools(
                server_id,
                remote_tools,
                foreign_tool_ids=foreign_tool_ids,
            )
            candidate_tools = {
                tool_id: tool
                for tool_id, tool in self._tools_by_id.items()
                if tool_id not in own_ids
            }
            candidate_tools.update(server_tools)
            candidate_descriptors = {
                **self._descriptors_by_server,
                server_id: descriptors,
            }
            self._publish_snapshot_locked(
                server_id=server_id,
                candidate_tools=candidate_tools,
                candidate_descriptors=candidate_descriptors,
            )

    # ------------------------------------------------------------------
    # 内部状态
    # ------------------------------------------------------------------

    def _publish_snapshot_locked(
        self,
        *,
        server_id: str | None,
        candidate_tools: dict[str, BaseTool],
        candidate_descriptors: dict[str, tuple[McpToolDescriptor, ...]],
    ) -> None:
        """在持有 _catalog_lock 的前提下切换目录状态并发布轻量事件。"""
        new_revision = _compute_catalog_revision(
            candidate_tools,
            candidate_descriptors,
        )
        previous_revision = (
            self._snapshot.revision if self._snapshot is not None else None
        )
        if previous_revision is not None and new_revision == previous_revision:
            self._publisher.publish(
                server_id=server_id,
                kind="unchanged",
                revision=new_revision,
                previous_revision=previous_revision,
            )
            return
        removed_ids = set(self._tools_by_id) - set(candidate_tools)
        kind = "tombstone" if removed_ids else "published"
        self._catalog_generation += 1
        self._tools_by_id = candidate_tools
        self._descriptors_by_server = candidate_descriptors
        self._snapshot = McpCatalogSnapshot(
            revision=new_revision,
            tools=MappingProxyType(dict(candidate_tools)),
        )
        self._publisher.publish(
            server_id=server_id,
            kind=kind,
            revision=new_revision,
            previous_revision=previous_revision,
        )
        if removed_ids:
            logger.info(
                "MCP 目录 tombstone: server_id=%s removed=%s revision=%s",
                server_id,
                sorted(removed_ids),
                new_revision,
            )

    def _build_server_snapshots(self) -> tuple[McpServerSnapshot, ...]:
        """现读 server 连接状态视图（不属于不可变目录语义）。"""
        snapshots: list[McpServerSnapshot] = []
        for server in self._servers:
            runtime = self._runtimes.get(server.server_id)
            connected = runtime is not None
            snapshots.append(
                McpServerSnapshot(
                    server_id=server.server_id,
                    transport=server.transport,
                    enabled=server.enabled,
                    status="ready" if server.enabled and connected else "disabled",
                    tools=self._descriptors_by_server.get(server.server_id, ()),
                    last_relist_error=
                    runtime.last_relist_error if runtime is not None else None,
                )
            )
        return tuple(snapshots)

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("McpCatalogOwner 尚未启动")
