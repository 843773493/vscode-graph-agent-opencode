from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, closing
from datetime import UTC, datetime
from typing import ClassVar
from uuid import uuid4

from app.abstractions.job_service import JobServiceProtocol
from app.agents.graph_binding import DEEP_AGENT_GRAPH_BINDING
from app.core.exceptions import NotFoundError
from app.core.phase_observer import observe_phase
from app.core.session_catalog_resolver import (
    SessionCatalogNodeProjection,
    SessionCatalogPathResolver,
    SessionCatalogSessionProjection,
)
from app.core.session_catalog_store import SessionCreationPurpose, SessionCreationRecord
from app.core.session_catalog_store.contracts import (
    SessionGenerationOriginDTO,
    SessionKind,
)
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore, ThreadGraphBindingRecord
from app.core.session_creation import ForkCompletionProof, SessionCreationService
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
    SessionOperationLease,
)
from app.core.workspace_identity import validate_workspace_id
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.common import CursorPage
from app.schemas.internal_v2.session import (
    ChildThreadListDTO,
    ChildThreadSummaryDTO,
    SessionControlResultDTO,
    SessionCreateRequest,
    SessionDTO,
    SessionListResultDTO,
    SessionUpdateRequest,
    TitleSource,
)
from app.schemas.internal_v2.trace import TraceEventDTO
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.mapping.trace_event_mapper import TraceEventMapper


class SessionService:
    DEFAULT_SESSION_TITLES: ClassVar[set[str]] = {"", "新会话", "未命名"}

    def __init__(
        self,
        *,
        config_service: ConfigService,
        trace_event_store: TraceEventStore,
        workspace_id: str,
        path_resolver: SessionCatalogPathResolver,
        creation_service: SessionCreationService,
    ):
        self._workspace_id = validate_workspace_id(workspace_id)
        self._config_service = config_service
        self._trace_event_store = trace_event_store
        self._path_resolver = path_resolver
        self._creation_service = creation_service
        self._navigation_topology_gate = NavigationTopologyGate(
            path_resolver.sessions_root
        )
        self._session_lifecycle_gate = SessionLifecycleGate(path_resolver.sessions_root)
        self._path_resolver.initialize()
        self._job_service: JobServiceProtocol | None = None
        self._change_listeners: list[Callable[[str, str], None]] = []

    @property
    def path_resolver(self) -> SessionCatalogPathResolver:
        return self._path_resolver

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def register_change_listener(self, listener: Callable[[str, str], None]) -> None:
        self._change_listeners.append(listener)

    def bind_job_service(self, job_service: JobServiceProtocol) -> None:
        self._job_service = job_service

    def _notify_changed(self, action: str, session_id: str) -> None:
        for listener in tuple(self._change_listeners):
            listener(action, session_id)

    @classmethod
    def _infer_created_title_source(
        cls,
        title: str | None,
    ) -> TitleSource:
        if (title or "").strip() in cls.DEFAULT_SESSION_TITLES:
            return "default"
        return "user"

    def _assert_workspace_binding(self, session: SessionDTO) -> None:
        if session.workspace_id != self._workspace_id:
            raise RuntimeError(
                "会话与当前工作区后端标识不一致: "
                f"session_id={session.session_id}, "
                f"session_workspace_id={session.workspace_id}, "
                f"backend_workspace_id={self._workspace_id}"
            )

    @staticmethod
    def _assert_session_node_active(node: SessionCatalogNodeProjection) -> None:
        if node.kind != "session":
            raise RuntimeError(
                "Session 准入要求 catalog session 节点: "
                f"session_id={node.node_id!r}, kind={node.kind!r}"
            )
        if node.state != "active":
            raise SessionDeletionPendingError(
                "session_deletion_pending: catalog owner 正在删除，"
                f"拒绝新业务准入: session_id={node.node_id!r}, state={node.state!r}"
            )

    def assert_session_active(self, session_id: str) -> None:
        """拒绝删除中的 owner 进入新业务准入，目录与清理读取仍可见。"""
        try:
            node = self._path_resolver.get_node(session_id)
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error
        self._assert_session_node_active(node)

    @asynccontextmanager
    async def session_read_admission(self, session_id: str) -> AsyncIterator[None]:
        """冷读的全部 handle 持有 shared guard；删除不能提前标记或隔离。"""
        async with (
            self._navigation_topology_gate.shared(),
            self._session_lifecycle_gate.shared(session_id),
        ):
            self.assert_session_active(session_id)
            yield

    @asynccontextmanager
    async def _session_write_scope(
        self, session_id: str,
    ) -> AsyncIterator[SessionCatalogNodeProjection]:
        """共同短锁序；active 准入和原租约结算在各自入口裁决。"""
        async with AsyncExitStack() as session_gate:
            async with self._navigation_topology_gate.shared():
                await session_gate.enter_async_context(
                    self._session_lifecycle_gate.exclusive(session_id)
                )
                try:
                    node = self._path_resolver.get_node(session_id)
                except KeyError as error:
                    raise NotFoundError(
                        f"Session {session_id} not found"
                    ) from error
                if node.kind != "session":
                    raise NotFoundError(f"Session {session_id} not found")
            yield node

    @asynccontextmanager
    async def session_write_admission(self, session_id: str) -> AsyncIterator[None]:
        """准入单会话短写，并让删除 drain 等待已准入写入完成。"""
        async with self._session_write_scope(session_id) as node:
            self._assert_session_node_active(node)
            yield

    @asynccontextmanager
    async def session_lease_settlement(
        self, session_id: str, control: SessionControlStore, lease: SessionOperationLease,
    ) -> AsyncIterator[bool]:
        """持有原 operation holder 时结算原租约；返回是否仍可尝试 fresh binder。

        catalog deleting 允许原 claim/回执/终态落库；身份、hash、token 与本端
        fence 必须保持。调用方不得凭此创建新 Job，后者仍走 active 准入。
        """
        async with self._session_write_scope(session_id) as node:
            expected_path = self._path_resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME
            if control.database_path != expected_path:
                raise RuntimeError(f"原 lease 控制库不属于 Session: session_id={session_id}, path={control.database_path}")
            control.assert_lease_current(lease)
            if control.get_fence() != ("active", lease.captured_lifecycle_generation):
                raise RuntimeError(f"原 lease 的 Session fence 已失效: session_id={session_id}, lease_id={lease.lease_id}")
            yield node.state == "active"

    @asynccontextmanager
    async def session_context_control_admission(
        self, session_id: str, thread_id: str, *, operation_id: str,
        payload: Mapping[str, object], recovery_ref: str,
    ) -> AsyncIterator[None]:
        """独立 Skill/ToolSet 控制的短写；旧 generation lease 先于业务 mutation。"""
        details = {
            "session_id": session_id, "thread_id": thread_id,
            "kind": payload.get("kind"), "recovery_ref": recovery_ref,
        }
        await observe_phase(operation_id, "context.control.before_admission", details=details)
        async with self.session_write_admission(session_id):
            try:
                self._path_resolver.resolve_thread_node(session_id, thread_id)
            except KeyError as error:
                raise NotFoundError(
                    f"SessionThread 不存在: session_id={session_id}, thread_id={thread_id}"
                ) from error
            with closing(SessionControlStore(
                self._path_resolver.resolve_session_node(session_id) / "session-control.sqlite",
            )) as control:
                lease = control.create_or_get_lease(
                    operation_kind="context_control",
                    operation_identity=f"context-control:{operation_id}",
                    preimage_hash=sha256_jcs({
                        "session_id": session_id, "thread_id": thread_id,
                        "operation_id": operation_id, "input": dict(payload),
                    }).rsplit(":", 1)[1],
                    recovery_ref=recovery_ref,
                )
                if lease.state == "active":
                    lease = control.mark_lease_settling(
                        lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                    )
                if lease.state not in {"settling", "completed"}:
                    raise RuntimeError(
                        "context-control-recovery-failed: 原控制操作已经终结，拒绝重放: "
                        f"operation_id={operation_id}, state={lease.state}"
                    )
                try:
                    await observe_phase(operation_id, "context.control.admitted", details={
                        **details,
                        "lease_id": lease.lease_id,
                        "captured_lifecycle_generation": lease.captured_lifecycle_generation,
                    })
                    yield
                except BaseException:
                    if lease.state == "settling":
                        control.settle_lease(
                            lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                            outcome="failed",
                        )
                    raise
                else:
                    if lease.state == "settling":
                        control.settle_lease(
                            lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                            outcome="completed",
                        )
                    await observe_phase(operation_id, "context.control.settled", details={
                        **details, "lease_id": lease.lease_id,
                        "captured_lifecycle_generation": lease.captured_lifecycle_generation,
                    })

    def settle_abandoned_context_controls(self, session_id: str) -> int:
        """调用者已持 Session exclusive；遗留短写不能有仍存活的 holder。"""
        count = 0
        with closing(SessionControlStore(
            self._path_resolver.resolve_session_node(session_id) / "session-control.sqlite",
        )) as control:
            for lease in control.list_non_terminal_leases():
                if lease.operation_kind != "context_control" or not lease.operation_identity.startswith("context-control:"):
                    continue
                lease = control.takeover_lease(
                    lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                )
                lease = control.mark_lease_settling(
                    lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                )
                # 这些短写不持网络/运行时资源。进程退出后不能盲重放 Skill
                # 或新 assembly；保留原 input hash 与失败 lease，由原 Job
                # 的恢复合同报告失败。删除随后只清理该 owner 的已有事实。
                control.settle_lease(
                    lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                    outcome="failed",
                )
                count += 1
        return count

    async def recover_context_controls(self) -> int:
        """启动时按权威 catalog 定点终结崩溃遗留控制，不扫描物理目录。"""
        _, nodes = self._path_resolver.catalog_store.read_nodes_snapshot()
        count = 0
        for node in nodes:
            if node.kind != "session" or node.state != "active":
                continue
            async with (
                self._navigation_topology_gate.shared(),
                self._session_lifecycle_gate.exclusive(node.node_id),
            ):
                count += self.settle_abandoned_context_controls(node.node_id)
        return count

    async def get(self, session_id: str) -> SessionDTO:
        # 按 ID 的单节点查询（不触发全 catalog BFS）：单会话读取的工作量
        # 与目录规模无关。未登记会话的 KeyError 与其余缺失同样归为 NotFound。
        try:
            node = self._path_resolver.get_node(session_id)
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error
        if not isinstance(node, SessionCatalogSessionProjection):
            raise NotFoundError(f"Session {session_id} not found")
        path = self._path_resolver.catalog_store.resolve_session_locator(
            f"sessions/{node.storage_relative_path}"
        )
        if not path.is_dir() or path.is_symlink():
            raise RuntimeError(
                "会话物理目录缺失或不是普通目录，拒绝解析（fail closed）: "
                f"session_id={session_id}"
            )
        if not (path / "session.json").is_file():
            raise NotFoundError(f"Session {session_id} not found")
        return self.project_catalog_session(node)

    def project_catalog_session(
        self,
        node: SessionCatalogSessionProjection,
    ) -> SessionDTO:
        """从同一次 catalog 行快照生成完整 DTO，不读取 manifest 或 creation history。"""
        session_id = node.node_id
        session = SessionDTO(
            session_id=session_id,
            workspace_id=node.workspace_id,
            resource_identity=session_resource_identity(node.workspace_id, session_id),
            uri=session_resource_uri(node.workspace_id, session_id),
            scope="workspace",
            title=node.name,
            title_source=node.title_source,
            parent_session_id=node.parent_session_id,
            thread_id=node.main_thread_id,
            kind=node.session_kind,
            current_agent_id=node.current_agent_id,
            current_provider_id=node.current_provider_id,
            context_source_session_id=node.context_source_session_id,
            generation_origin=node.generation_origin,
            created_at=node.created_at,
            updated_at=node.updated_at,
        )
        self._assert_workspace_binding(session)
        if session.current_provider_id is None:
            session.current_provider_id = (
                self._config_service.resolve_agent_provider_id(session.current_agent_id)
            )
        return session

    async def resolve_thread(self, session_id: str, thread_id: str) -> str:
        await self.get(session_id)
        self._path_resolver.resolve_thread_node(session_id, thread_id)
        return thread_id

    async def resolve_main_thread(self, session_id: str) -> str:
        """解析 Session 的权威 main thread id（普通入口的唯一事实来源）。

        身份取自 catalog 冻结 ``main_thread_id``，缺指针或节点非 session
        时 fail closed；不得用 session_id 冒充 thread_id。
        """
        try:
            return self._path_resolver.main_thread_id(session_id)
        except KeyError as error:
            raise NotFoundError(f"Session {session_id} not found") from error

    async def list(
        self,
        workspace_id: str | None = None,
        skip: int = 0,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SessionListResultDTO:
        """轻量成员索引确定排序与 cursor；同一读事务只加载当前页完整业务行。"""
        page_nodes, total, next_cursor, has_more = self._path_resolver.session_list_page(
            skip=skip, limit=limit, cursor=cursor
        )
        return SessionListResultDTO(
            items=[self.project_catalog_session(node) for node in page_nodes],
            total=total,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def child_session_summary(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> tuple[int, list[str], bool]:
        """读取有界的逻辑子会话摘要，不为诊断快照加载全部会话详情。"""
        await self.get(session_id)
        return self._path_resolver.child_session_summary(
            session_id,
            limit=limit,
        )

    async def list_child_threads(self, session_id: str) -> ChildThreadListDTO:
        """列出 owner Session 的 durable child thread（8.5-B 权威投影）。

        数据来自 owner ``session-control.sqlite```：thread_catalog 的
        child row 是唯一可见性提交点产物，collaboration ledger/member 提供
        delegation 语义，initial execution intent 提供 admission 状态。
        旧 delegated child Session 链路已下线，不再读 manifest。
        """
        await self.get(session_id)
        async with self.session_read_admission(session_id):
            control_database = (
                self._path_resolver.resolve_session_node(session_id)
                / "session-control.sqlite"
            )
            if not control_database.is_file():
                # 无控制库 = 该 Session 尚无 child thread（不建库、零副作用）。
                return ChildThreadListDTO(parent_session_id=session_id, items=[], total=0)
            with closing(SessionControlStore(control_database)) as control, control.read_transaction():
                threads = control.list_child_thread_rows()
                members = {
                    member.child_thread_id: member
                    for member in control.list_collaboration_members(
                        coordinator_session_id=session_id
                    )
                }
                intents = {
                    intent.thread_id: intent
                    for intent in control.list_initial_execution_intents()
                }
                forks = {
                    record.child_thread_id: record
                    for record in control.list_fork_thread_creations(state="published")
                }
            items: list[ChildThreadSummaryDTO] = []
            for thread in threads:
                self._path_resolver.resolve_thread_node(session_id, thread.thread_id)
                member = members.get(thread.thread_id)
                intent = intents.get(thread.thread_id)
                fork = forks.get(thread.thread_id)
                items.append(
                    ChildThreadSummaryDTO(
                        thread_id=thread.thread_id,
                        created_at=thread.created_at,
                        delegation_id=(
                            member.delegation_id if member is not None else None
                        ),
                        role=member.role if member is not None else None,
                        subagent_type=(
                            member.subagent_type if member is not None else None
                        ),
                        title=(
                            member.title if member is not None else
                            json.loads(fork.request_intent_json)["display_title"]
                            if fork is not None and fork.request_intent_json is not None else None
                        ),
                        collaboration_state=(member.state if member is not None else None),
                        admission_state=(intent.state if intent is not None else None),
                        report_execution_id=intent.execution_binding_id if intent is not None else None,
                        status=(
                            "running"
                            if intent is not None and intent.state == "bound"
                            else "failed"
                            if member is not None and member.state == "cancelled"
                            else "idle" if fork is not None else "pending"
                        ),
                    )
                )
            items.sort(key=lambda item: (item.created_at, item.thread_id), reverse=True)
            return ChildThreadListDTO(
                parent_session_id=session_id,
                items=items,
                total=len(items),
            )

    async def create(self, session: SessionCreateRequest) -> SessionDTO:
        return await self._create(
            title=session.title,
            title_source=self._infer_created_title_source(session.title),
            agent_id=session.agent_id,
            parent_node_id=session.folder_id,
        )

    async def prepare_context_fork(
        self,
        *,
        idempotency_key: str,
        title: str,
        agent_id: str,
        parent_session_id: str | None,
        context_source_session_id: str,
        generation_origin: SessionGenerationOriginDTO | None = None,
        parent_node_id: str | None = None,
        request_intent: dict[str, object] | None = None,
        fork_delivery_request_id: str | None = None,
    ) -> tuple[SessionDTO, SessionCreationRecord]:
        record = await self._prepare_creation(
            idempotency_key=idempotency_key,
            title=title,
            title_source="auto",
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            context_source_session_id=context_source_session_id,
            kind="context_fork",
            generation_origin=generation_origin,
            parent_node_id=parent_node_id,
            purpose="context_fork",
            request_intent=request_intent,
            fork_delivery_request_id=fork_delivery_request_id,
        )
        session = (
            await self.get(record.session_id)
            if record.state == "published" else self._prepared_session_from_record(record)
        )
        return session, record

    async def publish_prepared_context_fork(
        self,
        record: SessionCreationRecord,
        *,
        fork_completion: ForkCompletionProof,
        on_published: Callable[[SessionCreationRecord], None] | None = None,
    ) -> SessionDTO:
        result = await self._creation_service.publish_prepared(
            record,
            fork_completion=fork_completion,
            on_published=on_published,
        )
        session = await self.get(result.session_id)
        self._notify_changed("create", result.session_id)
        return session

    async def abort_prepared_context_fork(
        self,
        record: SessionCreationRecord,
        reason: str,
    ) -> SessionCreationRecord:
        return await self._creation_service.abort_prepared(record, reason)

    async def resume_prepared_context_fork(self, record: SessionCreationRecord) -> SessionCreationRecord:
        """用原 journal envelope 重入唯一 creation owner，不重新选择模型或读取当前配置。"""
        if record.purpose != "context_fork" or record.request_intent_json is None:
            raise ValueError("context fork 恢复缺少原 creation intent")
        return await self._creation_service.prepare(
            idempotency_key=record.session_creation_idempotency_key,
            title=record.display_name, title_source=record.title_source,
            parent_node_id=record.parent_node_id, graph_binding=record.graph_binding,
            session_metadata=json.loads(record.session_metadata_json), purpose=record.purpose,
            request_intent=json.loads(record.request_intent_json),
            fork_delivery_request_id=record.fork_delivery_request_id,
        )

    def list_preparing_creation_records(
        self,
        *,
        purpose: SessionCreationPurpose,
    ) -> list[SessionCreationRecord]:
        return self._creation_service.list_preparing_creation_records(
            purpose=purpose
        )

    async def create_generated(
        self,
        *,
        title: str,
        agent_id: str | None,
        parent_session_id: str | None,
        generation_origin: SessionGenerationOriginDTO,
        parent_node_id: str | None = None,
    ) -> SessionDTO:
        return await self._create(
            title=title,
            title_source="auto",
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            generation_origin=generation_origin,
            parent_node_id=parent_node_id,
        )

    async def _create(
        self,
        *,
        title: str | None,
        title_source: TitleSource,
        agent_id: str | None,
        parent_session_id: str | None = None,
        generation_origin: SessionGenerationOriginDTO | None = None,
        parent_node_id: str | None = None,
    ) -> SessionDTO:
        idempotency_key = f"session-{uuid4().hex}"
        record = await self._prepare_creation(
            idempotency_key=idempotency_key,
            title=title,
            title_source=title_source,
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            generation_origin=generation_origin,
            parent_node_id=parent_node_id,
        )
        try:
            result = await self._creation_service.publish_prepared(record)
        except RuntimeError as error:
            current = self._path_resolver.catalog_store.get_creation_record(idempotency_key)
            if current.state != "published":
                await self._creation_service.abort_prepared(record, str(error))
            raise
        session = await self.get(result.session_id)
        self._notify_changed("create", result.session_id)
        return session

    async def _prepare_creation(
        self,
        *,
        idempotency_key: str,
        title: str | None,
        title_source: TitleSource,
        agent_id: str | None,
        parent_session_id: str | None = None,
        context_source_session_id: str | None = None,
        kind: SessionKind = "normal",
        generation_origin: SessionGenerationOriginDTO | None = None,
        parent_node_id: str | None = None,
        purpose: SessionCreationPurpose = "normal",
        request_intent: dict[str, object] | None = None,
        fork_delivery_request_id: str | None = None,
    ) -> SessionCreationRecord:
        if self._config_service is None:
            raise RuntimeError("SessionService 未绑定 ConfigService")
        config_service = self._config_service
        resolved_agent_id = config_service.resolve_new_session_agent_id(agent_id)
        resolved_provider_id = config_service.resolve_new_session_provider_id(
            resolved_agent_id
        )
        await self._validate_parent_session(
            session_id=idempotency_key,
            workspace_id=self._workspace_id,
            parent_session_id=parent_session_id,
        )

        resolved_parent_node_id = parent_node_id
        navigation_parent_session_id = self._path_resolver.nearest_session_ancestor(
            parent_node_id
        )
        if parent_session_id is not None:
            if parent_node_id is None:
                resolved_parent_node_id = parent_session_id
                navigation_parent_session_id = parent_session_id
            elif navigation_parent_session_id != parent_session_id:
                raise ValueError(
                    "会话导航父节点与最近祖先会话不一致: "
                    f"parent_session_id={parent_session_id}, "
                    f"navigation_parent_session_id={navigation_parent_session_id}"
                )
        else:
            parent_session_id = navigation_parent_session_id

        resolved_title = title or "新会话"
        return await self._creation_service.prepare(
            idempotency_key=idempotency_key,
            title=resolved_title,
            title_source=title_source,
            parent_node_id=resolved_parent_node_id,
            purpose=purpose,
            graph_binding=ThreadGraphBindingRecord(
                *DEEP_AGENT_GRAPH_BINDING.selector_fields()
            ),
            request_intent=request_intent,
            fork_delivery_request_id=fork_delivery_request_id,
            session_metadata={
                "kind": kind,
                "delegation": None,
                "generation_origin": (
                    generation_origin.model_dump(mode="json")
                    if generation_origin is not None
                    else None
                ),
                "current_agent_id": resolved_agent_id,
                "current_provider_id": resolved_provider_id,
                "context_source_session_id": context_source_session_id,
            },
        )

    def _prepared_session_from_record(
        self,
        record: SessionCreationRecord,
    ) -> SessionDTO:
        if record.state != "preparing":
            raise RuntimeError(f"准备态 DTO 只允许冻结的 preparing intent: {record.session_id}, state={record.state}")
        metadata = json.loads(record.session_metadata_json)
        if not isinstance(metadata, dict):
            raise RuntimeError(  # noqa: TRY004 - journal 数据损坏属于语义冲突。
                "SessionCreationRecord metadata 必须是 JSON 对象: "
                f"session_id={record.session_id}"
            )
        generation_origin_data = metadata["generation_origin"]
        generation_origin = (
            SessionGenerationOriginDTO.model_validate(generation_origin_data)
            if generation_origin_data is not None
            else None
        )
        created_at = datetime.fromisoformat(record.created_at)
        session_data = SessionDTO(
            session_id=record.session_id,
            workspace_id=self._workspace_id,
            resource_identity=session_resource_identity(
                self._workspace_id, record.session_id
            ),
            uri=session_resource_uri(self._workspace_id, record.session_id),
            scope="workspace",
            title=record.display_name,
            title_source=record.title_source,
            current_agent_id=metadata["current_agent_id"],
            current_provider_id=metadata["current_provider_id"],
            parent_session_id=self._path_resolver.nearest_session_ancestor(
                record.parent_node_id
            ),
            context_source_session_id=metadata["context_source_session_id"],
            kind=metadata["kind"],
            generation_origin=generation_origin,
            created_at=created_at,
            updated_at=created_at,
            thread_id=record.main_thread_id,
        )
        return session_data

    async def update(
        self, session_id: str, session: SessionUpdateRequest
    ) -> SessionDTO:
        """在 catalog 原事务内更新 Agent/Provider，返回完整权威投影。"""
        async with self.session_write_admission(session_id):
            existing = await self.get(session_id)

            if session.agent_id is not None:
                if self._config_service is None:
                    raise RuntimeError("SessionService 未绑定 ConfigService")
                self._config_service.validate_agent_id(session.agent_id)

            update_data = session.model_dump(exclude_unset=True)
            target_agent_id = update_data.get("agent_id", existing.current_agent_id)
            requested_provider_id = update_data.get("provider_id")
            if "agent_id" in update_data and "provider_id" not in update_data:
                requested_provider_id = None
            if "agent_id" in update_data or "provider_id" in update_data:
                update_data["current_provider_id"] = (
                    self._config_service.resolve_agent_provider_id(
                        target_agent_id,
                        requested_provider_id,
                    )
                )
            self._path_resolver.catalog_store.update_session_configuration(
                session_id,
                current_agent_id=target_agent_id,
                current_provider_id=update_data.get(
                    "current_provider_id",
                    self._path_resolver.get_node(session_id).current_provider_id,
                ),
                updated_at=datetime.now(UTC),
            )
            updated = await self.get(session_id)

        self._notify_changed("update", session_id)
        return updated

    async def _validate_parent_session(
        self,
        *,
        session_id: str,
        workspace_id: str,
        parent_session_id: str | None,
    ) -> None:
        if parent_session_id is None:
            return
        if parent_session_id == session_id:
            raise ValueError("会话不能绑定到自身")

        ancestor_id: str | None = parent_session_id
        visited: set[str] = set()
        while ancestor_id is not None:
            if ancestor_id == session_id:
                raise ValueError("会话绑定会形成循环父子关系")
            if ancestor_id in visited:
                raise RuntimeError(f"现有会话树包含循环关系: session_id={ancestor_id}")
            visited.add(ancestor_id)
            try:
                ancestor = await self.get(ancestor_id)
            except NotFoundError as exc:
                raise ValueError(f"父会话不存在: {ancestor_id}") from exc
            if ancestor.workspace_id != workspace_id:
                raise ValueError("父子会话必须属于同一个工作区")
            ancestor_id = ancestor.parent_session_id

    async def control(
        self, session_id: str, action: str, payload: dict[str, object] | None = None
    ) -> SessionControlResultDTO:
        await self.get(session_id)
        return SessionControlResultDTO(
            session_id=session_id, action=action, status="executed"
        )

    async def list_trace_events(
        self,
        session_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> CursorPage[TraceEventDTO]:
        await self.get(session_id)
        page = self._trace_event_store.read_trace_page(
            session_id,
            cursor=cursor,
            limit=limit,
        )
        mapper = TraceEventMapper()
        return CursorPage(
            items=mapper.map_many(
                [event.model_dump() for event in page.events],
                session_id=session_id,
            ),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    async def ensure_trace_cursor(
        self, session_id: str, after_event_id: str | None
    ) -> None:
        await self.get(session_id)
        self._trace_event_store.ensure_cursor(session_id, after_event_id)

    async def stream_trace_events(
        self, session_id: str, after_event_id: str | None = None
    ):
        await self.get(session_id)
        mapper = TraceEventMapper()
        async for record in self._trace_event_store.stream_events(
            session_id,
            after_event_id,
        ):
            dto = mapper.map_one(record.event.model_dump(), session_id=session_id)
            if dto is not None:
                yield dto, record.cursor
