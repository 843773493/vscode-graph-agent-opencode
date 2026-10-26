"""MCP 工具目录唯一 owner（OpenSpec C8-B / M01 基础）。

职责边界：

- 连接生命周期：经 ``create_session`` + ``session_kwargs.message_handler`` 建立
  各 enabled server 的唯一 session（``MultiServerMCPClient.session()`` 不暴露
  message handler，而底层是同一个 ``create_session`` 工厂，不产生第二套连接实现）。
- 目录 relist：``list_mcp_tools`` 经 SDK 完整分页读取并验证 ``tools/list``。
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
from _thread import RLock
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, TypeAlias

from httpx import HTTPStatusError
from langchain_core.tools import BaseTool

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.core.identifier import create_uuid_hex
from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.events.channel_events import McpCatalogEventPublisher
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp.config import (
    parse_mcp_server_configs,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingRef,
    ExtensionTargetBinding,
    ExtensionTargetBindingInput,
    ExtensionTargetResolutionError,
    build_extension_catalog_binding,
    extension_schema_hash,
    payload_digest,
)
from app.services.infrastructure.mcp.naming import build_mcp_tool_id
from app.services.infrastructure.mcp.session_runtime import (
    McpChangeNotifyCallback,
    McpServerRuntime,
    McpSessionFactory,
    production_mcp_session_factory,
)
from mcp.shared.exceptions import McpError

if TYPE_CHECKING:
    from app.services.infrastructure.resource_platform.registry.semantic_registry import (
        ResourceRegistryCapture,
    )

logger = logging.getLogger(__name__)

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
    # 最近一次 relist 的错误事实；原始异常仅留诊断，成功 relist 后清空。
    last_relist_error: str | None = None


@dataclass(frozen=True, slots=True)
class McpCatalogSnapshot:
    """不可变目录快照：一个 revision 与它对应的全部工具对象。

    server 连接状态是运行时视图，由 :meth:`McpCatalogOwner.list_servers` 现读，
    不进入不可变目录语义。
    """

    revision: str
    tools: Mapping[str, BaseTool]


@dataclass(frozen=True, slots=True)
class McpCatalogPublication:
    "Owner 向 Registry 发布的完整不可变值；读线程不拼接可变目录字段。"

    binding_ref: ExtensionCatalogBindingRef
    descriptors: tuple[McpToolDescriptor, ...]
    tools: Mapping[str, BaseTool]
    status: Literal["available", "dirty", "unavailable"]
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"available", "dirty", "unavailable"}:
            raise ValueError("MCP publication status 非法")
        if (self.status == "available") != (self.error is None):
            raise ValueError("MCP publication readiness 与 error 不一致")


McpPublicationCallback: TypeAlias = Callable[[McpCatalogPublication], None]


def _catalog_failure_text(
    error: BaseException, *, server_id: str | None, stage: str,
) -> str:
    """普通出口只发布类型和数值事实，不能复制异常自由正文或路径。"""
    failures: list[str] = []
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        facts = [type(current).__name__]
        if isinstance(current, OSError):
            for name in ("errno", "winerror"):
                value = getattr(current, name, None)
                if type(value) is int:
                    facts.append(f"{name}={value}")
        if isinstance(current, McpError):
            facts.append(f"rpc_code={current.error.code}")
        if isinstance(current, HTTPStatusError):
            facts.append(f"http_status={current.response.status_code}")
        failures.append(" ".join(facts))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(current.exceptions))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return f"MCP 目录失败: server_id={server_id} stage={stage} errors=[{'; '.join(failures)}]"


def _compute_catalog_revision(
    tools_by_id: Mapping[str, BaseTool],
    descriptors_by_server: Mapping[str, tuple[McpToolDescriptor, ...]],
) -> str:
    """语义目录载荷的 sha256 revision（canonical JSON；与事件合同同形状）。"""
    payload: list[list[object]] = []
    for server_id in sorted(descriptors_by_server):
        server_entries: list[list[object]] = []
        for descriptor in sorted(
            descriptors_by_server[server_id], key=lambda item: item.tool_id
        ):
            tool = tools_by_id[descriptor.tool_id]
            server_entries.append(
                [
                    descriptor.tool_id,
                    descriptor.remote_name,
                    descriptor.description,
                    export_model_tool_json_schema(tool),
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
    connection_ref: str,
    foreign_tool_ids: set[str],
) -> tuple[dict[str, BaseTool], tuple[McpToolDescriptor, ...]]:
    """把远端工具适配为带 server 命名空间的 LangChain 工具与目录描述。"""
    tools: dict[str, BaseTool] = {}
    descriptors: list[McpToolDescriptor] = []
    for remote_tool in remote_tools:
        # 完整目录发布前验证方言、引用及结构；失败不得发布半目录。
        export_model_tool_json_schema(remote_tool)
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
            "mcp_connection_ref": connection_ref,
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
        self._session_scope: LifetimeScope | None = None
        self._started = False
        self._snapshot: McpCatalogSnapshot | None = None
        self._tools_by_id: dict[str, BaseTool] = {}
        self._descriptors_by_server: dict[str, tuple[McpToolDescriptor, ...]] = {}
        # 目录 generation lease：revision 每实际推进一次单调 +1；unchanged 不推进。
        self._catalog_generation = 0
        self._connection_generation = 0
        self._instance_id = create_uuid_hex(namespace="mcp-owner")
        self._runtimes: dict[str, McpServerRuntime] = {}
        self._catalog_lock = asyncio.Lock()
        self._pending_relist_tasks: set[asyncio.Task[None]] = set()
        self._publication_lock = RLock()
        self._publication: McpCatalogPublication | None = None
        self._publication_subscribers: dict[int, McpPublicationCallback] = {}
        self._next_publication_subscriber = 0
        self._dirty_servers: set[str] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._started or self._session_scope is not None:
            raise RuntimeError("McpCatalogOwner 不允许重复启动")
        session_scope = LifetimeScope("mcp-sessions")
        self._session_scope = session_scope
        failed_server_id: str | None = None
        try:
            for server in self._servers:
                if not server.enabled:
                    continue
                failed_server_id = server.server_id
                self._connection_generation += 1
                generation = self._connection_generation
                runtime = McpServerRuntime(
                    server=server,
                    generation=generation,
                    session_factory=self._session_factory,
                    on_change=self._make_tools_list_changed_callback(server.server_id, generation),
                    on_failure=self._handle_session_failure, scope=session_scope,
                )
                with self._publication_lock:
                    self._runtimes[server.server_id] = runtime
                await runtime.start()
                await self._apply_relist((runtime,), source="startup")
            if self._snapshot is None:
                # 目录为空（零 server / 全 disabled）也发布固定 envelope/revision。
                async with self._catalog_lock:
                    if self._snapshot is None:
                        self._publish_snapshot_locked(
                            server_id=None,
                            candidate_tools={},
                            candidate_descriptors={},
                        )
        except (Exception, asyncio.CancelledError) as error:
            logger.exception("MCP Server 启动或工具目录发现失败: server_id=%s", failed_server_id)
            failure: BaseException = error
            try:
                await session_scope.close()
                self._session_scope = None
            except ExceptionGroup as cleanup_error:
                failure = BaseExceptionGroup("MCP 启动与释放均失败", [error, cleanup_error])
            finally:
                self._runtimes.clear()
                self._tools_by_id.clear()
                self._descriptors_by_server.clear()
                self._publication = None
                self._snapshot = None
                self._dirty_servers.clear()
            if isinstance(error, asyncio.CancelledError):
                if failure is error:
                    raise
                raise failure
            raise McpCatalogError(
                _catalog_failure_text(failure, server_id=failed_server_id, stage="startup")
            ) from failure
        self._started = True

    async def shutdown(self) -> None:
        errors: list[Exception] = []
        with self._publication_lock:
            self._started = False
            if self._publication is not None:
                try:
                    self._publish_readiness_locked("unavailable", "MCP 目录 owner 已关闭")
                except ExceptionGroup as error:
                    errors.append(error)
            self._runtimes.clear()
            self._tools_by_id.clear()
            self._descriptors_by_server.clear()
            self._publication = None
            self._snapshot = None
            self._dirty_servers.clear()
        for task in tuple(self._pending_relist_tasks):
            task.cancel()
        if self._pending_relist_tasks:
            await asyncio.gather(*tuple(self._pending_relist_tasks), return_exceptions=True)
        if self._session_scope is not None:
            try:
                await self._session_scope.close()
            except Exception as error:
                logger.exception("MCP 生命周期或 publication callback 失败")
                errors.append(error)
            else:
                self._session_scope = None
        if errors:
            raise ExceptionGroup("MCP Owner 关闭失败", errors)

    # ------------------------------------------------------------------
    # 消费面（Agent / API / 健康检查）
    # ------------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    @property
    def catalog_channel_name(self) -> str:
        return self._publisher.channel_name

    @property
    def publication_lock(self) -> RLock:
        """runtime Registry 直接复用 Owner 的唯一发布锁。"""
        return self._publication_lock

    def validate_resource_capture(
        self, capture: ResourceRegistryCapture,
    ) -> None:
        """拒绝损坏的 stale available；正常 unavailable 保留 Coordinator 边界等待。"""
        publication = self._publication
        current_members = {} if publication is None else {
            member.member_id: member for member in publication.binding_ref.members
        }
        for snapshot in capture.snapshots:
            if snapshot.resource_kind != "mcp_tool_catalog" or not snapshot.available:
                continue
            owner_revision = dict(snapshot.source_lineage).get("mcp-owner-catalog:v1")
            if (
                not self._started
                or publication is None
                or publication.status != "available"
                or snapshot.generation != publication.binding_ref.generation
                or owner_revision != publication.binding_ref.catalog_revision
                or any(member.member_kind == "mcp" and current_members.get(member.member_id) != member
                       for member in snapshot.members)
            ):
                raise McpCatalogError(
                    "MCP Registry available snapshot 与唯一 Owner publication 不一致: "
                    f"resource_id={snapshot.resource_id}, generation={snapshot.generation}, "
                    f"owner_status={publication.status if publication else 'closed'}"
                )

    def publication_snapshot(self) -> McpCatalogPublication:
        "在读线程与发布线程间原子取得唯一完整值，不读取文件或网络。"

        with self._publication_lock:
            self._require_started()
            if self._publication is None:
                raise McpCatalogError("MCP 目录 owner 缺少已发布事实")
            return self._publication

    def subscribe_publication(
        self, callback: McpPublicationCallback,
    ) -> Callable[[], None]:
        "在同一发布锁内登记并立即交付完整值；释放归调用方 LifetimeScope。"

        if not callable(callback):
            raise TypeError("MCP publication callback 必须可调用")
        with self._publication_lock:
            publication = self.publication_snapshot()
            callback(publication)
            subscriber_id = self._next_publication_subscriber
            self._next_publication_subscriber += 1
            self._publication_subscribers[subscriber_id] = callback

        def unsubscribe() -> None:
            with self._publication_lock:
                self._publication_subscribers.pop(subscriber_id, None)

        return unsubscribe

    def get_tools(self) -> list[BaseTool]:
        return list(self.publication_snapshot().tools.values())

    def resolve_execution_target(self, binding: ExtensionTargetBinding) -> BaseTool:
        """只按封存连接及 schema 解析；新同名工具绝不能接管旧调用。"""
        # TODO(4.2/4.4): 接入调用资格及账本后，在有界排空期保留原 handle；
        # 当前原连接关闭即明确拒绝，不能用新同名连接补齐历史执行。
        with self._publication_lock:
            runtime = self._runtimes.get(binding.server_id or "")
            target = self._tools_by_id.get(binding.target_id)
            if (binding.origin != "mcp" or not self._started or runtime is None
                    or not runtime.running or target is None
                    or binding.connection_ref != self._connection_ref(runtime)
                    or (target.metadata or {}).get("mcp_connection_ref") != binding.connection_ref):
                raise ExtensionTargetResolutionError(
                    f"sealed target 的原连接执行 handle 已不可用: {binding.target_id}"
                )
            if extension_schema_hash(export_model_tool_json_schema(target)) != binding.schema_hash:
                raise ExtensionTargetResolutionError(
                    f"sealed target 与原连接当前参数 schema 不一致: {binding.target_id}"
                )
            return target

    def _connection_ref(self, runtime: McpServerRuntime) -> str:
        return f"mcp-connection:v1:{self._instance_id}:{runtime.generation}"

    def get_tool_ids(self) -> frozenset[str]:
        return frozenset(self.publication_snapshot().tools)

    def list_servers(self) -> list[McpServerSnapshot]:
        with self._publication_lock:
            self._require_started()
            return list(self._build_server_snapshots())

    def catalog_revision(self) -> str:
        return self.publication_snapshot().binding_ref.catalog_revision

    def catalog_generation(self) -> int:
        "目录 generation 由唯一不可变 publication 携带；unchanged 不推进。"
        return self.publication_snapshot().binding_ref.generation

    def binding_snapshot(self) -> ExtensionCatalogBindingRef:
        "完整不可变 publication 已冻结成员/参数 schema，不在消费时重建。"
        return self.publication_snapshot().binding_ref

    def tool_descriptors(self) -> tuple[McpToolDescriptor, ...]:
        return self.publication_snapshot().descriptors

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
        """显式目录刷新边界 relist 无通知 server；model-call preparation 不调用。"""
        self._require_started()
        await self._apply_relist(
            tuple(self._runtimes[server_id] for server_id in self.servers_without_change_notifications()),
            source="activation",
        )

    # ------------------------------------------------------------------
    # 通知与 relist
    # ------------------------------------------------------------------

    def _handle_session_failure(self, runtime: McpServerRuntime, error: BaseException) -> None:
        logger.error("MCP session 生命周期异常: server_id=%s", runtime.server.server_id, exc_info=error)
        with self._publication_lock:
            if self._runtimes.get(runtime.server.server_id) is not runtime:
                return
            text = _catalog_failure_text(error, server_id=runtime.server.server_id, stage="session")
            runtime.last_relist_error = text
            self._dirty_servers.add(runtime.server.server_id)
            if self._publication is not None:
                self._publish_readiness_locked("unavailable", text)

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
        with self._publication_lock:
            if not self._started:
                return
            runtime = self._runtimes.get(server_id)
            if runtime is None or not runtime.running or runtime.generation != generation:
                logger.info(
                    "忽略过期 MCP tools/list_changed: server_id=%s callback_generation=%d",
                    server_id, generation,
                )
                return
            runtime.relist_sequence += 1
            relist_sequence = runtime.relist_sequence
            self._mark_dirty(server_id)
        task = asyncio.create_task(
            self._apply_relist((runtime,), source="notification", relist_sequence=relist_sequence)
        )
        self._pending_relist_tasks.add(task)
        task.add_done_callback(self._pending_relist_tasks.discard)

    async def _apply_relist(
        self, runtimes: tuple[McpServerRuntime, ...], *, source: str,
        relist_sequence: int | None = None,
    ) -> None:
        "同一次刷新只发布一个完整 candidate；失败保留整个旧 valid。"

        if not runtimes:
            return
        server_ids = tuple(runtime.server.server_id for runtime in runtimes)
        # 显式刷新共享原目录锁，重叠请求不能把已过期结果当作成功回执。
        async with self._catalog_lock:
            with self._publication_lock:
                if source != "startup":
                    self._require_started()
                leases: dict[str, tuple[int, int]] = {}
                for runtime in runtimes:
                    if relist_sequence is None:
                        runtime.relist_sequence += 1
                    leases[runtime.server.server_id] = (
                        runtime.generation,
                        runtime.relist_sequence if relist_sequence is None else relist_sequence,
                    )
                if self._started:
                    self._dirty_servers.update(server_ids)
                    self._publish_readiness_locked("dirty", f"MCP 目录正在完整刷新: servers={server_ids}")

            def current() -> bool:
                return all(
                    self._runtimes.get(runtime.server.server_id) is runtime
                    and runtime.running
                    and (runtime.generation, runtime.relist_sequence) == leases[runtime.server.server_id]
                    for runtime in runtimes
                )

            failed_server_id = server_ids[0]
            try:
                remote_by_server: dict[str, list[BaseTool]] = {}
                for runtime in runtimes:
                    failed_server_id = runtime.server.server_id
                    remote_by_server[failed_server_id] = await runtime.session.list_tools()
                with self._publication_lock:
                    if not current():
                        raise McpCatalogRelistError(f"MCP relist 结果已过期: servers={server_ids}")
                    replaced_ids = {
                        descriptor.tool_id
                        for server_id in server_ids
                        for descriptor in self._descriptors_by_server.get(server_id, ())
                    }
                    candidate_tools = {
                        tool_id: tool
                        for tool_id, tool in self._tools_by_id.items()
                        if tool_id not in replaced_ids
                    }
                    candidate_descriptors = dict(self._descriptors_by_server)
                    for server_id in server_ids:
                        failed_server_id = server_id
                        server_tools, descriptors = _adapt_server_tools(
                            server_id, remote_by_server[server_id],
                            connection_ref=self._connection_ref(self._runtimes[server_id]),
                            foreign_tool_ids=set(candidate_tools),
                        )
                        candidate_tools.update(server_tools)
                        candidate_descriptors[server_id] = descriptors
                    self._dirty_servers.difference_update(server_ids)
                    self._publish_snapshot_locked(
                        server_id=server_ids[0] if len(server_ids) == 1 else None,
                        candidate_tools=candidate_tools,
                        candidate_descriptors=candidate_descriptors,
                    )
                    for runtime in runtimes:
                        runtime.last_relist_error = None
            except Exception as error:
                with self._publication_lock:
                    if not current():
                        if source == "notification":
                            logger.info("MCP relist 过期结果已丢弃: servers=%s", server_ids)
                            return
                        raise McpCatalogRelistError(
                            f"MCP relist 连接或请求代际已过期: servers={server_ids}"
                        ) from error
                    error_text = _catalog_failure_text(
                        error, server_id=failed_server_id, stage="relist",
                    )
                    self._runtimes[failed_server_id].last_relist_error = error_text
                    self._dirty_servers.update(server_ids)
                    if self._publication is not None:
                        try:
                            self._publish_readiness_locked("unavailable", error_text)
                        except ExceptionGroup as invalidation_error:
                            raise ExceptionGroup(
                                "MCP relist 与失效广播失败", [error, invalidation_error]
                            ) from error
                logger.exception("MCP relist 失败，保留旧 valid 目录: servers=%s source=%s", server_ids, source)
                if source == "notification":
                    return
                raise McpCatalogRelistError(error_text) from error

    def _mark_dirty(self, server_id: str) -> None:
        with self._publication_lock:
            self._dirty_servers.add(server_id)
            if self._publication is not None:
                self._publish_readiness_locked(
                    "dirty", f"MCP 目录正在完整刷新: server_id={server_id}",
                )

    def _publish_readiness_locked(
        self, status: Literal["dirty", "unavailable"], error: str,
    ) -> None:
        previous = self._publication
        if previous is None:
            raise McpCatalogError("MCP readiness 缺少原 publication")
        publication = McpCatalogPublication(
            binding_ref=previous.binding_ref, descriptors=previous.descriptors,
            tools=previous.tools, status=status, error=error,
        )
        self._publication = publication
        errors = self._broadcast_publication_locked(publication)
        if errors:
            raise ExceptionGroup("MCP readiness 广播失败", errors)

    def _broadcast_publication_locked(
        self, publication: McpCatalogPublication,
    ) -> list[Exception]:
        errors: list[Exception] = []
        for callback in tuple(self._publication_subscribers.values()):
            try:
                callback(publication)
            except Exception as error:
                logger.exception("MCP 生命周期或 publication callback 失败")
                errors.append(error)
        return errors

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
        "唯一发布锁内构造完整值并切换；事件只携带 revision，不注册资源。"

        with self._publication_lock:
            new_revision = _compute_catalog_revision(
                candidate_tools, candidate_descriptors,
            )
            previous_revision = (
                self._snapshot.revision if self._snapshot is not None else None
            )
            unchanged = new_revision == previous_revision
            generation = self._catalog_generation + int(not unchanged)
            descriptors = tuple(sorted(
                (
                    descriptor
                    for entries in candidate_descriptors.values()
                    for descriptor in entries
                ),
                key=lambda item: item.tool_id,
            ))
            binding = build_extension_catalog_binding(
                catalog_revision=new_revision, generation=generation,
                targets=(
                    ExtensionTargetBindingInput(
                        target_id=descriptor.tool_id, origin="mcp",
                        server_id=descriptor.server_id,
                        connection_ref=self._connection_ref(self._runtimes[descriptor.server_id]),
                        input_schema=export_model_tool_json_schema(candidate_tools[descriptor.tool_id]),
                    )
                    for descriptor in descriptors
                ),
            )
            publication = McpCatalogPublication(
                binding_ref=binding, descriptors=descriptors,
                tools=MappingProxyType(dict(candidate_tools)),
                status="dirty" if self._dirty_servers else "available",
                error=(
                    f"MCP 目录仍有未完成刷新: {sorted(self._dirty_servers)}"
                    if self._dirty_servers else None
                ),
            )
            # 同锁内完成全部 subscriber 发布；任何失败先设置 Owner unavailable，
            # 再向所有 subscriber 广播失效。失败的失效回调由 capture validator 拒绝。
            errors = self._broadcast_publication_locked(publication)
            if errors:
                if self._publication is None:
                    self._publication = publication
                try:
                    self._publish_readiness_locked("unavailable", "MCP subscriber 发布失败")
                except ExceptionGroup as invalidation_error:
                    errors.append(invalidation_error)
                raise ExceptionGroup("MCP publication 广播失败", errors)
            removed_ids = set(self._tools_by_id) - set(candidate_tools)
            self._catalog_generation = generation
            self._tools_by_id = candidate_tools
            self._descriptors_by_server = candidate_descriptors
            self._snapshot = McpCatalogSnapshot(
                revision=new_revision, tools=publication.tools,
            )
            self._publication = publication
            self._publisher.publish(
                server_id=server_id,
                kind="unchanged" if unchanged else (
                    "tombstone" if removed_ids else "published"
                ),
                revision=new_revision, previous_revision=previous_revision,
            )
            if removed_ids:
                logger.info(
                    "MCP 目录 tombstone: server_id=%s removed=%s revision=%s",
                    server_id, sorted(removed_ids), new_revision,
                )

    def _build_server_snapshots(self) -> tuple[McpServerSnapshot, ...]:
        """现读 server 连接状态视图（不属于不可变目录语义）。"""
        snapshots: list[McpServerSnapshot] = []
        for server in self._servers:
            runtime = self._runtimes.get(server.server_id)
            connected = runtime is not None and runtime.running
            snapshots.append(
                McpServerSnapshot(
                    server_id=server.server_id,
                    transport=server.transport,
                    enabled=server.enabled,
                    status=("ready" if connected else "failed") if server.enabled else "disabled",
                    tools=self._descriptors_by_server.get(server.server_id, ()),
                    last_relist_error=
                    runtime.last_relist_error if runtime is not None else None,
                )
            )
        return tuple(snapshots)

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("McpCatalogOwner 尚未启动")
