from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.abstractions.job_service import JobServiceProtocol
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store.contracts import CatalogTransactionHook
from app.core.session_subtree_delete import SubtreeDeleteResult
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation.operations import (
    AutoSessionTitleCommand,
    NavigationEventsPageDTO,
    NavigationMutationEnqueueRequest,
    NavigationMutationEnqueueResultDTO,
    NavigationMutationIntentDTO,
    NavigationMutationStatusPageDTO,
    NavigationSnapshotDTO,
)
from app.services.business.session_navigation.executor import NavigationMutationExecutor
from app.services.business.session_navigation.operations_service import (
    NavigationAuthScope,
    SessionCatalogOperationsService,
    local_navigation_scope,
)
from app.services.business.session_navigation.queue_store import (
    NavigationMutationQueueStore,
    NavigationMutationRecord,
)
from app.services.business.session_navigation.read_service import (
    SessionCatalogReadService,
)
from app.services.business.session_service import SessionService


def _committed_node_id(record: NavigationMutationRecord) -> str:
    """返回已提交 operation 的结果 node ID；未提交即明确报错（不猜测）。"""
    if record.state != "committed" or record.result_node_id is None:
        raise RuntimeError(
            "导航 operation 未成功提交，无法取得结果节点 ID: "
            f"operation_id={record.operation_id}, state={record.state}, "
            f"error_code={record.error_code}, error_detail={record.error_detail}"
        )
    return record.result_node_id


def _raise_for_rejected(record: NavigationMutationRecord) -> None:
    """按既有错误分类重新抛出被拒绝的 operation（同步 API 的零回归契约）。

    既有同步端点把未知节点映射为 404、把形态/语义冲突映射为 400/409；这里按
    durable record 的 ``error_code`` 还原同一分类，避免调用方观察不到失败。
    """
    detail = record.error_detail or record.error_code or "导航 operation 被拒绝"
    if record.error_code == "node_not_found":
        raise KeyError(detail)
    if record.error_code in (
        "invalid_operation",
        "dependency_failed",
        "session_deletion_pending",
        "source_retained_by_fork",
        "source_retention_operation_pending",
    ):
        raise ValueError(detail)
    raise RuntimeError(detail)


@dataclass(frozen=True, slots=True)
class AutoSessionTitleMutationResult:
    outcome: Literal["applied", "not_applicable"]
    session: SessionDTO


class SessionCatalogService(SessionCatalogReadService):
    """把权威会话索引投影为可分页、可搜索的目录 API。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        job_service: JobServiceProtocol | None = None,
        background_task_registry: BackgroundTaskRegistry | None = None,
        operations_service: SessionCatalogOperationsService | None = None,
    ) -> None:
        super().__init__(session_service=session_service)
        self._job_service = job_service
        self._background_task_registry = background_task_registry
        # 未显式注入时按容器装配的同一 resolver/store 构造导航 operation 栈。
        self._operations_service = operations_service or self._build_operations()
        self._session_service.register_change_listener(self._on_session_changed)

    def _build_operations(self) -> SessionCatalogOperationsService | None:
        """构造导航 operation 栈（enqueue/worker/status/snapshot/events）。

        共享 ``SessionCatalogPathResolver`` 的同一 catalog store 与子树删除流，
        因此不会出现第二套写入实现或第二份 catalog 连接。resolver 不是 SQLite
        catalog 实现时（非生产装配）返回 None，相关入口显式报错。
        """
        resolver = self._path_resolver
        if not isinstance(resolver, SessionCatalogPathResolver):
            return None
        store = resolver.catalog_store
        # workspace 身份取 resolver 绑定值（catalog 行归属键）：与 catalog 写入
        # 同源，避免出现两个可能不一致的 workspace 口径。
        workspace_id = resolver.workspace_id
        queue = NavigationMutationQueueStore(store)
        executor = NavigationMutationExecutor(
            store=store,
            workspace_id=workspace_id,
            queue=queue,
            path_resolver=resolver,
            delete_runner=self._run_shared_delete,
        )
        return SessionCatalogOperationsService(
            store=store,
            workspace_id=workspace_id,
            queue=queue,
            executor=executor,
        )

    async def _run_shared_delete(
        self,
        idempotency_key: str,
        root_node_id: str,
        mark_transaction_hook: CatalogTransactionHook,
        finish_transaction_hook: CatalogTransactionHook,
    ) -> SubtreeDeleteResult:
        """以确定性 key 进入唯一子树删除流，关闭新 admission 后结算旧操作。

        JobService 只关闭新 Job/消息写入；既有 Job 不阻断 catalog deleting，
        由共享删除流的运行时 cleanup 与生命周期 lease owner 排空。
        """
        frozen_session_ids = self._path_resolver.descendant_session_ids(
            root_node_id,
            include_self=True,
        )

        if self._background_task_registry is not None:
            target_ids = set(frozen_session_ids)
            blockers = [
                handle
                for handle in self._background_task_registry.list_active_handles()
                if handle.session_id in target_ids
            ]
            if blockers:
                raise RuntimeError(
                    "会话存在运行中后台任务，拒绝递归删除: "
                    + ",".join(
                        f"{handle.session_id}:{handle.task_id}" for handle in blockers
                    )
                )

        async def run() -> SubtreeDeleteResult:
            return await self._path_resolver.delete_subtree(
                idempotency_key=idempotency_key,
                root_node_id=root_node_id,
                mark_transaction_hook=mark_transaction_hook,
                finish_transaction_hook=finish_transaction_hook,
            )

        if self._job_service is None:
            return await run()
        return await self._job_service.run_sessions_delete_operation(
            frozen_session_ids,
            run,
        )


    def bind_job_service(self, job_service: JobServiceProtocol) -> None:
        if self._job_service is not None:
            raise RuntimeError("SessionCatalogService 已绑定 JobService")
        self._job_service = job_service

    @property
    def path_resolver(self) -> SessionCatalogPathResolver:
        return self._path_resolver

    @property
    def workspace_id(self) -> str:
        """本工作区后端的权威 workspace 身份（导航 operation 幂等 scope 的一维）。"""
        return self._path_resolver.workspace_id

    def _on_session_changed(self, action: str, session_id: str) -> None:
        self.invalidate()


    async def recover_pending_subtree_deletes(self) -> list[SubtreeDeleteResult]:
        """启动恢复入口：按 SQLite 权威 record 幂等续跑未终结的子树删除。

        返回已收敛（drain+finish 完成）的删除结果。只依据
        ``subtree_delete_records`` 定点继续，不扫描磁盘、不吸收外部改动、
        不假回滚 active；任一 record 恢复失败即向上抛错，由启动期如实记录并
        继续启动（目录读取已对 deleting 节点隐藏，不再拖垮整个后端）。
        """
        results = await self.operations.recover_pending_subtree_deletes()
        if results:
            self.invalidate()
        return results

    async def ensure_folder_path(
        self,
        path_segments: list[str],
        *,
        parent_folder_id: str | None = None,
    ) -> str | None:
        parent_node_id = parent_folder_id
        intents: list[NavigationMutationIntentDTO] = []
        # 依赖链（created_by_operation_id）让执行侧按前序 committed
        # result_node_id 解析父节点，因此重建过程中不必猜测 canonical ID。
        previous_operation_id: str | None = None
        for raw_segment in path_segments:
            segment = raw_segment.strip()
            if not segment:
                raise ValueError("会话目录路径段不能为空")
            matches = self._existing_child_folders(parent_node_id, segment)
            if len(matches) > 1:
                raise RuntimeError(
                    f"物理目录存在同名兄弟文件夹: parent={parent_node_id}, name={segment}"
                )
            if matches:
                parent_node_id = matches[0].node_id
                previous_operation_id = None
                continue
            operation_id = create_prefixed_id("op")
            intents.append(
                NavigationMutationIntentDTO(
                    client_operation_id=operation_id,
                    client_sequence=len(intents) + 1,
                    kind="create_folder",
                    base_catalog_revision=self._catalog_revision(),
                    name=segment,
                    # 链式创建时父节点由 created_by_operation_id 在执行侧解析，
                    # 这里不能塞入尚未分配的空 ID。
                    parent_node_id=None if previous_operation_id else parent_node_id,
                    created_by_operation_id=previous_operation_id,
                )
            )
            previous_operation_id = operation_id
        if intents:
            records = await self._submit_batch(intents)
            parent_node_id = _committed_node_id(records[-1])
        self.invalidate()
        return parent_node_id or None

    async def auto_title_session(
        self,
        session_id: str,
        *,
        expected_title: str,
        title: str,
    ) -> AutoSessionTitleMutationResult:
        """提交内部默认来源 + 原标题 + revision CAS，并读取终态后的权威 Session。"""
        if expected_title not in SessionService.DEFAULT_SESSION_TITLES:
            raise ValueError(
                f"自动标题只能替换默认标题: session_id={session_id}, "
                f"expected_title={expected_title!r}"
            )
        if self.operations.node_kind(session_id) != "session":
            raise KeyError(f"会话不存在: {session_id}")
        command = AutoSessionTitleCommand(
            client_operation_id=create_prefixed_id("op"),
            base_catalog_revision=self._catalog_revision(),
            expected_revision=self.operations.node_revision(session_id),
            target_node_id=session_id,
            expected_title=expected_title,
            name=title,
        )
        scope = self._scope()
        await self.operations.enqueue_auto_session_title(command, scope)
        receipt = await self.operations.await_terminal(
            command.client_operation_id,
            scope,
        )
        session = await self._session_service.get(session_id)
        if receipt.state == "committed":
            self.invalidate()
            if session.title == title and session.title_source == "auto":
                return AutoSessionTitleMutationResult("applied", session)
            return AutoSessionTitleMutationResult("not_applicable", session)
        if receipt.error_code == "session_title_not_applicable":
            return AutoSessionTitleMutationResult("not_applicable", session)
        _raise_for_rejected(receipt)
        raise RuntimeError("自动标题队列返回了未知终态")

    def _existing_child_folders(
        self,
        parent_node_id: str | None,
        name: str,
    ) -> list:
        """返回父节点下同名 folder 投影（只读查询，不构成写路径）。"""
        return [
            node
            for node in self._path_resolver.list_nodes()
            if node.kind == "folder"
            and node.parent_node_id == parent_node_id
            and node.name == name
        ]

    # ------------------------------------------------------------------
    # 导航 mutation 单一写路径（同步 API 与异步 enqueue 共用）
    # ------------------------------------------------------------------

    @property
    def operations(self) -> SessionCatalogOperationsService:
        """异步导航 operation 的服务面（enqueue/status/snapshot/events）。"""
        if self._operations_service is None:
            raise RuntimeError(
                "SessionCatalogOperationsService 尚未在应用启动阶段初始化"
            )
        return self._operations_service

    async def submit_operation_batch(
        self,
        request: NavigationMutationEnqueueRequest,
        scope: NavigationAuthScope,
    ) -> NavigationMutationEnqueueResultDTO:
        """typed 批量入队（202 durable acceptance）。"""
        return await self.operations.enqueue(request, scope)

    def operation_status(
        self,
        operation_ids: list[str],
        scope: NavigationAuthScope,
    ) -> NavigationMutationStatusPageDTO:
        return self.operations.status(operation_ids, scope)

    def navigation_snapshot(self) -> NavigationSnapshotDTO:
        return self.operations.snapshot()

    def navigation_events(
        self,
        *,
        after: int,
        limit: int | None = None,
    ) -> NavigationEventsPageDTO:
        return self.operations.events(after=after, limit=limit)

    def decode_navigation_events_cursor(self, cursor: str) -> tuple[int, int]:
        """解析 navigation 事件 cursor：返回 (after, 签发时水位)。"""
        return self.operations.decode_events_cursor(cursor)

    async def drain_navigation_operations(self) -> None:
        """显式驱动一次 FIFO 排空（测试与同步 façade 使用）。"""
        await self.operations.drain_once()

    def _catalog_revision(self) -> int:
        return self._path_resolver.revision

    async def _submit_batch(
        self,
        intents: list[NavigationMutationIntentDTO],
    ) -> list[NavigationMutationRecord]:
        """提交一批 intent 并驱动到终态；返回按 ``client_sequence`` 的记录。

        同步 API 的所有写操作都经此入口，因此与原异步链路完全同一实现。批内
        任一 operation 未到达终态即明确报错（不伪造成功）。
        """
        request = NavigationMutationEnqueueRequest(intents=intents)
        await self.operations.enqueue(request, self._scope())
        records: list[NavigationMutationRecord] = []
        for intent in sorted(intents, key=lambda item: item.client_sequence):
            record = await self.operations.await_terminal(
                intent.client_operation_id, self._scope()
            )
            if record.state != "committed":
                # 同步 API 保留既有错误契约：拒绝原因按既有分类重新抛出，绝不
                # 把 rejected/dependency_failed 当成功返回。
                _raise_for_rejected(record)
            records.append(record)
        return records

    def _scope(self) -> NavigationAuthScope:
        """本地工作区后端的 operation 幂等 scope（gateway/actor 固定本地身份）。

        当前架构不存在可区分的第二认证主体；workspace 取后端自身身份，永不信任
        请求体自报。联邦/多主体接入时由 Gateway 认证后传入真实 peer/用户身份。
        """
        return local_navigation_scope(self._path_resolver.workspace_id)
