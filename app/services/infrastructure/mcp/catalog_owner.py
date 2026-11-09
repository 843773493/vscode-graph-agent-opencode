"""MCP 工具目录唯一 owner（OpenSpec C8-B / M01 基础）。

职责边界：

- 连接生命周期：由每 Server 原 task 经 SDK transport 和统一
  ``CancellableMcpClientSession`` 建立、排空并关闭唯一 session。
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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, TypeAlias

from httpx import HTTPStatusError
from langchain_core.tools import BaseTool

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.policy.tool_capability import metadata_for_tool
from app.core.identifier import create_uuid_hex
from app.core.lifecycle import LifetimeScope
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.core.workspace_identity import read_workspace_id
from app.schemas.gateway.service_plugin import PluginBinding
from app.schemas.internal_v2.config import McpCleanupDTO
from app.schemas.internal_v2.mcp import (
    McpPrivateHeaderRefDTO,
    McpPrivateHeaderWriteRequestDTO,
    McpServiceReferenceDTO,
    McpWorkspaceTargetDTO,
)
from app.services.infrastructure.config.contracts import (
    ConfigApplyRecorder,
    ConfigPublication,
)
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.config.workspace_plugins import (
    workspace_plugin_bindings_for_root,
)
from app.services.infrastructure.events.channel_events import (
    McpCatalogEventPublisher,
    ResourceStateEventPublisher,
)
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.config import (
    McpServerConfig,
    parse_mcp_server_configs,
    sealed_call_timeout_seconds,
)
from app.services.infrastructure.mcp.execution_handles import (
    McpExecutionHandle,
    McpExecutionHandles,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ConnectionReadiness,
    ExtensionCatalogBindingRef,
    ExtensionTargetBinding,
    ExtensionTargetBindingInput,
    ExtensionTargetResolutionError,
    build_extension_catalog_binding,
    payload_digest,
)
from app.services.infrastructure.mcp.naming import build_mcp_tool_id
from app.services.infrastructure.mcp.oauth import (
    McpOAuthProvider,
    is_authorization_required,
)
from app.services.infrastructure.mcp.private_inputs import McpPrivateInputStore
from app.services.infrastructure.mcp.retirement import McpConnectionRetirement
from app.services.infrastructure.mcp.session_runtime import (
    McpChangeNotifyCallback,
    McpServerRuntime,
    McpSessionFactory,
    production_mcp_session_factory,
)
from app.services.infrastructure.mcp.tool_definitions import (
    ToolDefinition,
    definition_from_tool,
)
from configs.plugin_parameters import plugin_parameters
from mcp.shared.exceptions import McpError

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService
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
    transport: Literal["streamable_http"]
    enabled: bool
    status: Literal["disabled", "ready", "failed", "authorization_required", "unavailable"]
    tools: tuple[McpToolDescriptor, ...]
    # 最近一次 relist 的错误事实；原始异常仅留诊断，成功 relist 后清空。
    last_relist_error: str | None = None
    connection_ref: str | None = None
    service: McpServiceReferenceDTO | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class McpManagementSnapshot:
    """同一 publication 锁内读取目录代际及连接状态，不另存管理目录。"""

    catalog_revision: str
    catalog_generation: int
    servers: tuple[McpServerSnapshot, ...]


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
    definitions: Mapping[str, ToolDefinition]
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
    definitions: Mapping[str, ToolDefinition],
    descriptors_by_server: Mapping[str, tuple[McpToolDescriptor, ...]],
) -> str:
    """语义目录载荷的 sha256 revision（canonical JSON；与事件合同同形状）。"""
    payload: list[list[object]] = []
    for server_id in sorted(descriptors_by_server):
        server_entries: list[list[object]] = []
        for descriptor in sorted(
            descriptors_by_server[server_id], key=lambda item: item.tool_id
        ):
            definition = definitions[descriptor.tool_id]
            server_entries.append(
                [
                    descriptor.tool_id,
                    descriptor.remote_name,
                    descriptor.description,
                    definition.input_schema,
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
        leases: ExternalResourceLeaseLedger,
        publication_lock: RLock | None = None,
        session_factory: McpSessionFactory | None = None,
        config_service: ConfigService | None = None,
        plugin_bindings: Sequence[PluginBinding] = (),
    ) -> None:
        self._config_service = config_service
        self._plugin_bindings = tuple(plugin_bindings)
        self._servers = parse_mcp_server_configs(raw_config, workspace_root=workspace_root)
        self._publisher = McpCatalogEventPublisher(event_service=event_service)
        self._workspace_root = workspace_root
        self._leases = leases
        self._session_factory: McpSessionFactory = (
            session_factory or self.oauth_session_factory(None)
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
        self._publication_lock = publication_lock if publication_lock is not None else RLock()
        self._retirement = McpConnectionRetirement(
            leases=leases, lock=self._publication_lock, timeout_seconds=sealed_call_timeout_seconds(raw_config),
            publisher=ResourceStateEventPublisher(event_service=event_service, owner_domain="mcp"),
        )
        self.execution_handles = McpExecutionHandles(
            leases=leases, lock=self._publication_lock, resolve_active=self._resolve_execution_handle,
            retention_error=self._retained_execution_error,
            timeout_seconds=sealed_call_timeout_seconds(raw_config),
        )
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
        await self.execution_handles.reconcile_lost_handles()
        # 只保留已核实关闭的旧连接；旧 socket 与未完成操作不能随新连接复活。
        await self._leases.reconcile({
            record.resource_id: "stopped"
            for record in self._leases.records_by_kind("mcp_connection")
            if record.status == "stopped"
        }, kind="mcp_connection")
        session_scope = LifetimeScope("mcp-sessions")
        self._session_scope = session_scope
        failed_server_id: str | None = None
        try:
            for server in self._servers:
                if not server.enabled or self._plugin_unavailable_reason(server, self._plugin_bindings) is not None:
                    continue
                failed_server_id = server.server_id
                runtime = self._new_runtime(server)
                with self._publication_lock:
                    self._runtimes[server.server_id] = runtime
                try:
                    await runtime.start()
                    await self._apply_relist((runtime,), source="startup")
                except Exception as error:
                    if not is_authorization_required(error):
                        raise
                    await runtime.scope.close()
                    continue
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
                # 取消本身作为主异常交还；cause不能再包含主异常形成环。
                failure = cleanup_error if isinstance(error, asyncio.CancelledError) else BaseExceptionGroup(
                    "MCP 启动与释放均失败", [error, cleanup_error],
                )
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
                raise error from failure
            raise McpCatalogError(
                _catalog_failure_text(failure, server_id=failed_server_id, stage="startup")
            ) from failure
        self._started = True
        self.execution_handles.start()

    async def shutdown(self) -> None:
        errors: list[Exception] = []
        with self._publication_lock:
            self._started = False
            try:
                self.execution_handles.close()
            except Exception as error:  # noqa: BLE001 - 汇总账本释放失败，仍须排空 SDK 原 task，最后重抛。
                errors.append(error)
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
                self._retirement.clear_closed()
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

    def _resolve_execution_handle(self, binding: ExtensionTargetBinding) -> McpExecutionHandle:
        """只按封存连接及 schema 解析；新同名工具绝不能接管旧调用。"""
        with self._publication_lock:
            handle = self._active_execution_handle(binding)
            if handle is None:
                raise ExtensionTargetResolutionError(
                    f"sealed target 的原连接执行 handle 已不可用: {binding.target_id}"
                )
            # 完整契约在同锁的目录发布时已验证；执行准入只比较已发布摘要。
            if self.publication_snapshot().definitions[binding.target_id].schema_hash != binding.schema_hash:
                raise ExtensionTargetResolutionError(
                    f"sealed target 与原连接当前参数 schema 不一致: {binding.target_id}"
                )
            return handle

    def _active_execution_handle(self, binding: ExtensionTargetBinding) -> McpExecutionHandle | None:
        """调用方持有 publication lock，只解析精确的 active 连接对象。"""
        runtime = self._runtimes.get(binding.server_id or "")
        target = self._tools_by_id.get(binding.target_id)
        if (binding.origin != "mcp" or not self._started or runtime is None
                or self._retirement.switching_plugin_service(runtime.server.server_id)
                or not runtime.running or target is None
                or binding.connection_ref != self._connection_ref(runtime)
                or (target.metadata or {}).get("mcp_connection_ref") != binding.connection_ref):
            return None
        return McpExecutionHandle(binding, target, runtime)

    def _retained_execution_error(self, binding: ExtensionTargetBinding) -> str | None:
        """正常替换只放行原有资格；当前目录 relist 仍核 schema 撤换。"""
        with self._publication_lock:
            retained = self._retirement.retained(binding.connection_ref)
            if (self._started and retained is not None and retained.running
                    and any(server.server_id == binding.server_id and server.enabled
                            and self._plugin_unavailable_reason(server, self._plugin_bindings) is None
                            for server in self._servers)):
                return None
            active = self._active_execution_handle(binding)
            if active is None:
                return f"sealed target 的原连接执行 handle 已不可用: {binding.target_id}"
            if self.publication_snapshot().definitions[binding.target_id].schema_hash != binding.schema_hash:
                return f"sealed target 与原连接当前参数 schema 不一致: {binding.target_id}"
            return None

    async def write_private_header(
        self, request: McpPrivateHeaderWriteRequestDTO, *, actual_target: McpWorkspaceTargetDTO,
    ) -> McpPrivateHeaderRefDTO:
        """私有写入只分配不可变引用，配置生效仍由原来源候选/CAS 链负责。"""
        return await asyncio.to_thread(
            McpPrivateInputStore(workspace_root=self._workspace_root).write,
            server_id=request.server_id, header_name=request.header_name,
            endpoint=request.endpoint, value=request.value,
        )

    async def prepare_config(
        self, raw_config: object, scope: LifetimeScope, _record_side_effect: ConfigApplyRecorder,
        *, plugin_bindings: Sequence[PluginBinding],
        reconnect_server_ids: frozenset[str] = frozenset(),
    ) -> tuple[ConfigPublication, frozenset[str]]:
        """按 Server 准备候选 Gateway 服务连接，再共同发布。"""
        self._require_started()
        servers = parse_mcp_server_configs(raw_config, workspace_root=self._workspace_root)
        proposed = {server.server_id: server for server in servers}
        bindings = tuple(plugin_bindings)
        prepared: dict[str, McpServerRuntime] = {}
        tools: dict[str, BaseTool] = {}
        descriptors: dict[str, tuple[McpToolDescriptor, ...]] = {}
        sequences: dict[str, int] = {}
        published = False
        catalog_claimed = False
        switch_guard = ExitStack()

        def release_catalog() -> None:
            nonlocal catalog_claimed
            if catalog_claimed:
                catalog_claimed = False
                self._catalog_lock.release()

        async def close_unpublished() -> None:
            try:
                if not published:
                    errors = await asyncio.gather(*(runtime.scope.close() for runtime in prepared.values()), return_exceptions=True)
                    failures = [error for error in errors if isinstance(error, BaseException)]
                    if failures:
                        raise BaseExceptionGroup("MCP 候选连接释放失败", failures)
            finally:
                switch_guard.close()
                release_catalog()

        scope.register(close_unpublished, label="mcp-unpublished-candidate")
        # 与原 relist 串行准备，避免未变 Server 在冲突检查后增加同名工具。
        # 锁随候选 scope 回收或同步提交释放，网络 I/O 不持有 publication 锁。
        await self._catalog_lock.acquire()
        catalog_claimed = True
        with self._publication_lock:
            self._require_started()
            previous = {server.server_id: server for server in self._servers}
            changed = {key for key in previous.keys() | proposed.keys()
                       if previous.get(key) != proposed.get(key)
                       or (key in proposed and self._plugin_unavailable_reason(proposed[key], self._plugin_bindings)
                           != self._plugin_unavailable_reason(proposed[key], bindings))}
            candidate_bindings = {(item.package_id, item.plugin_id, item.workspace_id): item for item in bindings}
            for server_id in reconnect_server_ids:
                server = proposed.get(server_id)
                if server is None or server.service is None or not server.enabled:
                    continue
                service = server.service
                binding = candidate_bindings.get((service.package_id, service.plugin_id, service.workspace_id))
                if binding is None or not binding.enabled or binding.package_version != service.package_version:
                    raise ConfigConflictError(f"MCP Server {server_id} 的候选插件绑定已关闭或版本不匹配")
            changed.update(reconnect_server_ids)
            switch_guard.enter_context(self._retirement.reserve_plugin_service_switch(
                self._servers, servers, affected_server_ids=reconnect_server_ids,
                require_settled_server_ids=reconnect_server_ids,
            ))
            self._retirement.require_replaceable(changed)
            unchanged_ids = {key for key, tool in self._tools_by_id.items()
                             if (tool.metadata or {}).get("mcp_server_id") not in changed}
        assert self._session_scope is not None
        owner_scope = self._session_scope
        for key in sorted(changed):
            server = proposed.get(key)
            if (server is None or not server.enabled
                    or self._plugin_unavailable_reason(server, bindings) is not None):
                continue
            runtime = self._new_runtime(server)
            prepared[key] = runtime
            try:
                await runtime.start()
                sequences[key] = runtime.relist_sequence
                remote_tools = await runtime.session.list_tools()
                server_tools, descriptors[key] = _adapt_server_tools(
                    key, remote_tools, connection_ref=self._connection_ref(runtime), foreign_tool_ids=unchanged_ids | tools.keys(),
                )
            except Exception as error:
                logger.exception("MCP 配置候选连接准备失败: server_id=%s", key)
                raise McpCatalogError(_catalog_failure_text(error, server_id=key, stage="configuration")) from error
            tools.update(server_tools)

        def publish() -> None:
            try:
                activate()
            finally:
                switch_guard.close()
                release_catalog()

        def activate() -> None:
            nonlocal published
            with self._publication_lock:
                self._require_started()
                if published or self._servers != tuple(previous.values()):
                    raise McpCatalogError("MCP 候选的原配置基线已失效")
                self._retirement.require_replaceable(changed)
                replaced = [runtime for key, runtime in self._runtimes.items() if key in changed]
                candidate_tools = {key: tool for key, tool in self._tools_by_id.items()
                                   if (tool.metadata or {}).get("mcp_server_id") not in changed}
                candidate_tools.update(tools)
                candidate_descriptors = {key: value for key, value in self._descriptors_by_server.items() if key not in changed}
                candidate_descriptors.update(descriptors)
                self._runtimes = {key: value for key, value in self._runtimes.items() if key not in changed} | prepared
                self._servers = servers
                self._plugin_bindings = bindings
                self._dirty_servers.difference_update(changed)
                invalidated = {key for key, runtime in prepared.items()
                               if runtime.running and runtime.relist_sequence != sequences[key]}
                self._dirty_servers.update(invalidated)
                published = True
                # 所有权始终属于原 Owner 的每 Server scope；后续任意配置 shadow
                # 释放只能回收未提交候选，不能关闭未变化的 active 连接。
                for key in changed:
                    if (key not in proposed or not proposed[key].enabled
                            or self._plugin_unavailable_reason(proposed[key], bindings) is not None):
                        self._retirement.revoke_server(key)
                for runtime in replaced:
                    if not runtime.scope.is_closed:
                        self._retirement.retire(self._connection_ref(runtime), runtime,
                            admits_sealed=runtime.server.server_id in prepared, owner_scope=owner_scope)
                self._publish_snapshot_locked(server_id=None, candidate_tools=candidate_tools,
                                              candidate_descriptors=candidate_descriptors)
                for key in invalidated:
                    runtime = prepared[key]
                    if runtime.running:
                        self._handle_tools_list_changed(key, runtime.generation)

        return publish, frozenset(unchanged_ids | tools.keys())

    @contextmanager
    def guard_configuration(self, previous_config: object, candidate_config: object) -> Iterator[None]:
        """来源候选准备期间冻结受影响代际；关闭不等待旧调用，替换要求结清。"""
        if not isinstance(previous_config, Mapping) or not isinstance(candidate_config, Mapping):
            raise TypeError("MCP 配置 guard 必须取得完整 Workspace 配置")
        previous_document = previous_config
        candidate_document = candidate_config
        previous_servers = parse_mcp_server_configs(previous_document.get("mcp"), workspace_root=self._workspace_root)
        servers = parse_mcp_server_configs(candidate_document.get("mcp"), workspace_root=self._workspace_root)
        affected_server_ids, require_settled_server_ids = self._plugin_configuration_switches(
            previous_document, candidate_document, previous_servers, servers,
        )
        with ExitStack() as guard:
            with self._publication_lock:
                self._require_started()
                guard.enter_context(self._retirement.reserve_plugin_service_switch(
                    previous_servers, servers, affected_server_ids=affected_server_ids,
                    require_settled_server_ids=require_settled_server_ids,
                ))
            yield

    def plugin_configuration_switches(self, previous_config: Mapping[str, object],
                                      candidate_config: Mapping[str, object]) -> tuple[set[str], set[str]]:
        previous_servers = parse_mcp_server_configs(previous_config.get("mcp"), workspace_root=self._workspace_root)
        candidate_servers = parse_mcp_server_configs(candidate_config.get("mcp"), workspace_root=self._workspace_root)
        return self._plugin_configuration_switches(
            previous_config, candidate_config, previous_servers, candidate_servers,
        )

    def _plugin_configuration_switches(
        self, previous_config: Mapping[str, object], candidate_config: Mapping[str, object],
        previous_servers: Sequence[McpServerConfig], candidate_servers: Sequence[McpServerConfig],
    ) -> tuple[set[str], set[str]]:
        previous_bindings = workspace_plugin_bindings_for_root(
            previous_config, workspace_root=self._workspace_root,
        )
        candidate_bindings = workspace_plugin_bindings_for_root(
            candidate_config, workspace_root=self._workspace_root,
        )
        previous_by_key = {(item.package_id, item.plugin_id, item.workspace_id): item for item in previous_bindings}
        candidate_by_key = {(item.package_id, item.plugin_id, item.workspace_id): item for item in candidate_bindings}
        previous_parameters = plugin_parameters(previous_config)
        candidate_parameters = plugin_parameters(candidate_config)
        parameter_keys = {
            (package_id, plugin_id)
            for package_id in previous_parameters.keys() | candidate_parameters.keys()
            for plugin_id in (
                previous_parameters.get(package_id, {}).keys()
                | candidate_parameters.get(package_id, {}).keys()
            )
            if previous_parameters.get(package_id, {}).get(plugin_id)
            != candidate_parameters.get(package_id, {}).get(plugin_id)
        }
        changed_binding_keys = {
            key for key in previous_by_key.keys() | candidate_by_key.keys()
            if previous_by_key.get(key) != candidate_by_key.get(key)
        }
        workspace_id = read_workspace_id(self._workspace_root) if self._workspace_root is not None else None
        affected_keys = changed_binding_keys | {
            (package_id, plugin_id, workspace_id)
            for package_id, plugin_id in parameter_keys
            if workspace_id is not None
        }
        before_servers = {server.server_id: server for server in previous_servers}
        after_servers = {server.server_id: server for server in candidate_servers}
        affected: set[str] = set()
        require_settled: set[str] = set()
        for server_id in before_servers.keys() | after_servers.keys():
            server = after_servers.get(server_id) or before_servers[server_id]
            service = server.service
            if service is None:
                continue
            key = (service.package_id, service.plugin_id, service.workspace_id)
            if key not in affected_keys:
                continue
            affected.add(server_id)
            old_binding, new_binding = previous_by_key.get(key), candidate_by_key.get(key)
            parameter_changed = (service.package_id, service.plugin_id) in parameter_keys
            binding_version_changed = (
                old_binding is not None and new_binding is not None
                and old_binding.package_version != new_binding.package_version
            )
            if (parameter_changed and old_binding is not None and new_binding is not None
                    and old_binding.enabled and new_binding.enabled) or (
                    binding_version_changed and old_binding is not None and new_binding is not None
                    and old_binding.enabled and new_binding.enabled
            ):
                require_settled.add(server_id)
        return affected, require_settled

    def server_configuration(self, server_id: str) -> McpServerConfig:
        with self._publication_lock:
            self._require_started()
            for server in self._servers:
                if server.server_id == server_id:
                    return server
        raise LookupError("MCP Server 未配置")

    def oauth_session_factory(self, provider: McpOAuthProvider | None) -> McpSessionFactory:
        """交互授权只覆盖本次回调，其余连接依赖仍来自同一 owner。"""
        return partial(
            production_mcp_session_factory, workspace_root=self._workspace_root,
            config_service=self._config_service, oauth_provider=provider,
        )

    def _new_runtime(self, server: McpServerConfig, session_factory: McpSessionFactory | None = None) -> McpServerRuntime:
        """启动、配置替换和显式连接共用唯一 task／scope 创建入口。"""
        with self._publication_lock:
            if self._session_scope is None:
                raise RuntimeError("MCP Owner 缺少连接生命周期 scope")
            self._connection_generation += 1
            generation = self._connection_generation
            return McpServerRuntime(
                server=server, generation=generation, connection_ref=f"mcp-connection:v1:{self._instance_id}:{generation}",
                session_factory=session_factory or self._session_factory,
                on_change=self._make_tools_list_changed_callback(server.server_id, generation),
                on_failure=self._handle_session_failure, on_started=self._handle_session_started,
                on_preparing=self._handle_session_preparing, on_closed=self._handle_session_closed,
                scope=self._session_scope.child(f"server:{server.server_id}:{generation}"),
            )

    async def connect_server(
        self, server_id: str, *, admit: Callable[[], None],
        session_factory: McpSessionFactory | None = None, on_initialized: Callable[[], None],
    ) -> None:
        """用户等待在目录锁之外；只在核实目录后原子发布精确新连接。"""
        server = self.server_configuration(server_id)
        if not server.enabled:
            raise PermissionError("MCP Server 已在配置中禁用")
        unavailable = self._plugin_unavailable_reason(server, self._plugin_bindings)
        if unavailable is not None:
            raise PermissionError(unavailable)
        with self._publication_lock:
            admit()
            original = self._runtimes.get(server_id)
            self._retirement.require_replaceable({server_id})
        if original is not None and original.running:
            on_initialized()
            await self._apply_relist((original,), source="connect")
            with self._publication_lock:
                admit()
                if self._runtimes.get(server_id) is not original or not original.running:
                    raise McpCatalogError("MCP connect 的原连接已改变")
            return
        if original is not None and not original.scope.is_closed:
            async with asyncio.timeout(self._retirement.timeout_seconds):
                await original.scope.close()
        runtime = self._new_runtime(server, session_factory)
        published = False
        try:
            await runtime.start()
            on_initialized()
            async with self._catalog_lock:
                sequence = runtime.relist_sequence
                remote_tools = await runtime.session.list_tools()
                with self._publication_lock:
                    admit()
                    if (self.server_configuration(server_id) != server or self._runtimes.get(server_id) is not original
                            or self._plugin_unavailable_reason(server, self._plugin_bindings) is not None
                            or not runtime.running or sequence != runtime.relist_sequence):
                        raise McpCatalogError("MCP connect 的配置、连接或完整目录准备已过期")
                    self._retirement.require_replaceable({server_id})
                    tools = {key: value for key, value in self._tools_by_id.items()
                             if (value.metadata or {}).get("mcp_server_id") != server_id}
                    added, descriptors = _adapt_server_tools(
                        server_id, remote_tools, connection_ref=self._connection_ref(runtime), foreign_tool_ids=set(tools),
                    )
                    tools.update(added)
                    self._runtimes[server_id] = runtime
                    self._dirty_servers.discard(server_id)
                    published = True
                    self._publish_snapshot_locked(server_id=server_id, candidate_tools=tools,
                        candidate_descriptors={**self._descriptors_by_server, server_id: descriptors})
        except BaseException as original:
            if not published:
                try:
                    await run_cleanup_shielded(runtime.scope.close)
                except BaseException as failure:
                    raise original from failure
            raise

    async def disconnect_server(self, server_id: str, *, admit: Callable[[], None]) -> None:
        """先撤销执行准入、保留已知契约，再核实 active 与旧退役连接实际关闭。"""
        async with self._catalog_lock:
            with self._publication_lock:
                self.server_configuration(server_id)
                admit()
                self._retirement.revoke_server(server_id)
                runtime = self._runtimes.get(server_id)
                runtimes = self._retirement.runtimes_for_server(server_id)
                if runtime is not None:
                    runtimes = (*runtimes, runtime)
                for item in runtimes:
                    item.request_close()
                tools = {key: value for key, value in self._tools_by_id.items()
                         if (value.metadata or {}).get("mcp_server_id") != server_id}
                self._dirty_servers.discard(server_id)
                publication_error: Exception | None = None
                try:
                    self._publish_snapshot_locked(
                        server_id=server_id, candidate_tools=tools,
                        candidate_descriptors=dict(self._descriptors_by_server),
                        readiness_overrides={server_id: "disconnected"},
                    )
                except Exception as error:  # noqa: BLE001 - 广播失败仍须核实原连接关闭，随后合并报告。
                    publication_error = error
        errors = [] if publication_error is None else [publication_error]
        for item in runtimes:
            try:
                async with asyncio.timeout(self._retirement.timeout_seconds):
                    await item.scope.close()
            except Exception as error:  # noqa: BLE001 - 不能因一个连接失败遗弃同 Server 的其它代际。
                errors.append(error)
        if errors:
            raise ExceptionGroup("MCP 断开未完成", errors)

    def mark_authorization_required(self, server_id: str) -> None:
        """仅标准 SDK 的真实授权回调调用；未知目录不虚构成员。"""
        with self._publication_lock:
            self.server_configuration(server_id)
            runtime = self._runtimes.get(server_id)
            if runtime is not None and runtime.running:
                raise McpCatalogError("正在运行的 MCP 连接不能改为未授权目录")
            self._publish_snapshot_locked(
                server_id=server_id, candidate_tools=dict(self._tools_by_id),
                candidate_descriptors=dict(self._descriptors_by_server),
                readiness_overrides={server_id: "authorization_required"},
            )

    async def _handle_session_preparing(self, runtime: McpServerRuntime) -> None:
        await run_sync_shielded(
            self._leases.register_external, resource_id=self._connection_ref(runtime),
            kind="mcp_connection", lifetime_scope="workspace", initial_status="unknown",
            mcp_server_id=runtime.server.server_id,
        )

    async def _handle_session_started(self, runtime: McpServerRuntime) -> None:
        await run_sync_shielded(
            self._leases.update_external_state, self._connection_ref(runtime),
            lifetime_scope="workspace", status="running",
        )

    async def _handle_session_closed(self, runtime: McpServerRuntime, verified: bool) -> None:
        resource_id = self._connection_ref(runtime)
        if self._leases.get(resource_id) is not None:
            await run_sync_shielded(
                self._leases.update_external_state, resource_id,
                lifetime_scope="workspace", status="stopped" if verified else "unknown",
                cleanup_failed=runtime.cleanup_failed,
            )

    def _connection_ref(self, runtime: McpServerRuntime) -> str:
        return runtime.connection_ref

    def get_tool_ids(self) -> frozenset[str]:
        return frozenset(self.publication_snapshot().tools)

    def list_servers(self) -> list[McpServerSnapshot]:
        return list(self.management_snapshot().servers)

    def management_snapshot(self) -> McpManagementSnapshot:
        with self._publication_lock:
            publication = self.publication_snapshot()
            return McpManagementSnapshot(
                catalog_revision=publication.binding_ref.catalog_revision,
                catalog_generation=publication.binding_ref.generation,
                servers=self._build_server_snapshots(),
            )

    def cleanup_diagnostics(self) -> tuple[McpCleanupDTO, ...]:
        """诊断不触发 relist，不影响已提交的配置或新连接 readiness。"""
        return self._retirement.diagnostics()

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
                if runtime.running and not runtime.session.supports_tool_list_changed()
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
            tools=previous.tools, definitions=previous.definitions, status=status, error=error,
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
        readiness_overrides: Mapping[str, ConnectionReadiness] | None = None,
    ) -> None:
        "唯一发布锁内构造完整值并切换；事件只携带 revision，不注册资源。"

        with self._publication_lock:
            definitions: dict[str, ToolDefinition] = {}
            for descriptors in candidate_descriptors.values():
                for descriptor in descriptors:
                    tool = candidate_tools.get(descriptor.tool_id)
                    if tool is not None:
                        metadata = metadata_for_tool(tool, origin="mcp")
                        definitions[descriptor.tool_id] = definition_from_tool(tool, metadata)
                    else:
                        if self._publication is None:
                            raise McpCatalogError("未知 MCP 目录不能补造定义")
                        previous = self._publication.definitions[descriptor.tool_id]
                        readiness = (readiness_overrides or {}).get(descriptor.server_id, previous.readiness)
                        if readiness == "ready":
                            raise McpCatalogError("已知 MCP 定义缺少执行连接及明确的未就绪状态")
                        definitions[descriptor.tool_id] = replace(previous, readiness=readiness)
            new_revision = _compute_catalog_revision(
                definitions, candidate_descriptors,
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
                        connection_ref=(self._connection_ref(self._runtimes[descriptor.server_id])
                                        if definitions[descriptor.tool_id].readiness == "ready" else None),
                        input_schema=definitions[descriptor.tool_id].input_schema,
                        readiness=definitions[descriptor.tool_id].readiness,
                    )
                    for descriptor in descriptors
                ),
            )
            publication = McpCatalogPublication(
                binding_ref=binding, descriptors=descriptors,
                tools=MappingProxyType(dict(candidate_tools)),
                definitions=MappingProxyType(definitions),
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

    @staticmethod
    def _plugin_unavailable_reason(server: McpServerConfig, bindings: Sequence[PluginBinding]) -> str | None:
        """沿同次配置发布的精确绑定判准入；配置存在不代表插件已经开启。"""
        service = server.service
        if service is None:
            return None
        binding = next((item for item in bindings
                        if (item.package_id, item.plugin_id, item.workspace_id)
                        == (service.package_id, service.plugin_id, service.workspace_id)), None)
        if binding is None:
            return "MCP 服务对应的工作区插件尚未绑定"
        if not binding.enabled:
            return "MCP 服务对应的工作区插件已关闭"
        if binding.package_version != service.package_version:
            return "MCP 服务版本与工作区插件绑定不一致"
        return None

    def _build_server_snapshots(self) -> tuple[McpServerSnapshot, ...]:
        """现读 server 连接状态视图（不属于不可变目录语义）。"""
        snapshots: list[McpServerSnapshot] = []
        for server in self._servers:
            runtime = self._runtimes.get(server.server_id)
            connection_ref = self._connection_ref(runtime) if runtime is not None and runtime.running else None
            connected = connection_ref is not None
            unavailable_reason = self._plugin_unavailable_reason(server, self._plugin_bindings)
            descriptors = self._descriptors_by_server.get(server.server_id, ())
            known_readiness = {
                self._publication.definitions[descriptor.tool_id].readiness
                for descriptor in descriptors
            } if self._publication is not None else set()
            snapshots.append(
                McpServerSnapshot(
                    server_id=server.server_id,
                    transport="streamable_http",
                    enabled=server.enabled,
                    status=(
                        "disabled" if not server.enabled else
                        "ready" if connected else
                        "unavailable" if unavailable_reason is not None else
                        "authorization_required" if "authorization_required" in known_readiness or runtime is not None and runtime.authorization_required else "failed"
                    ),
                    # 失败／关闭保留原描述；主动断开沿唯一 publication 的 readiness 隐去管理视图工具。
                    tools=descriptors if runtime is not None and "disconnected" not in known_readiness else (),
                    last_relist_error=
                    runtime.last_relist_error if runtime is not None else None,
                    connection_ref=connection_ref,
                    service=server.service,
                    unavailable_reason=unavailable_reason,
                )
            )
        return tuple(snapshots)

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("McpCatalogOwner 尚未启动")
