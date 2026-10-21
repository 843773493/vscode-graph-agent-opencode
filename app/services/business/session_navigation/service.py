from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from typing import Literal

from app.abstractions.job_service import JobServiceProtocol
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_resolver import (
    SessionCatalogNodeProjection,
    SessionCatalogPathResolver,
    SessionCatalogSessionProjection,
)
from app.core.session_catalog_store.contracts import CatalogTransactionHook
from app.core.session_subtree_delete import SubtreeDeleteResult
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogBreadcrumbDTO,
    SessionCatalogExportDTO,
    SessionCatalogNodeDTO,
    SessionCatalogPageDTO,
    SessionCatalogSearchResultDTO,
    SessionCatalogSearchResultsDTO,
)
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


@dataclass(frozen=True)
class _SessionCatalogReadSnapshot:
    catalog_revision: int
    revision: str
    nodes: list[SessionCatalogNodeDTO]
    nodes_by_id: dict[str, SessionCatalogNodeDTO]
    default_provider_nodes: list[SessionCatalogSessionProjection]
    search_paths: dict[str, str]


@dataclass(frozen=True, slots=True)
class AutoSessionTitleMutationResult:
    outcome: Literal["applied", "not_applicable"]
    session: SessionDTO


class SessionCatalogService:
    """把权威会话索引投影为可分页、可搜索的目录 API。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        job_service: JobServiceProtocol | None = None,
        background_task_registry: BackgroundTaskRegistry | None = None,
        operations_service: SessionCatalogOperationsService | None = None,
    ) -> None:
        self._session_service = session_service
        self._path_resolver: SessionCatalogPathResolver = session_service.path_resolver
        self._job_service = job_service
        self._background_task_registry = background_task_registry
        # 未显式注入时按容器装配的同一 resolver/store 构造导航 operation 栈。
        self._operations_service = operations_service or self._build_operations()
        self._cached_snapshot: _SessionCatalogReadSnapshot | None = None
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

    def invalidate(self) -> None:
        self._cached_snapshot = None

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

    async def refresh(self) -> SessionCatalogPageDTO:
        self.invalidate()
        snapshot = await self._snapshot(force=True)
        roots = self._sorted_children(snapshot.nodes, None)
        # 刷新语义是「重载根页并校验索引」：没有 limit/cursor 入参，必须返回
        # 完整根页且 cursor 为空，否则超过上限的根节点会既不在 items 里、也没
        # 有可继续分页的 cursor，被静默吞掉。分页由 list_children 承担。
        return self._build_page(
            roots,
            parent_node_id=None,
            offset=0,
            limit=None,
            revision=snapshot.revision,
            catalog_revision=snapshot.catalog_revision,
            cursor_scope=None,
        )

    async def list_children(
        self,
        *,
        parent_node_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> SessionCatalogPageDTO:
        snapshot = await self._snapshot()
        if parent_node_id is not None and not any(
            node.node_id == parent_node_id for node in snapshot.nodes
        ):
            raise KeyError(f"会话目录节点不存在: {parent_node_id}")
        children = self._sorted_children(snapshot.nodes, parent_node_id)
        # cursor 绑定发牌时的分页作用域（父节点）：同一个 revision 下把某个父节点的
        # cursor 拿去翻另一个父节点的页，会让 offset 落到错误切片的中间，静默跳项。
        scope = parent_node_id
        offset = self._decode_cursor(
            cursor,
            snapshot.revision,
            snapshot.catalog_revision,
            scope,
            collection_size=len(children),
        )
        return self._build_page(
            children,
            parent_node_id=parent_node_id,
            offset=offset,
            limit=limit,
            revision=snapshot.revision,
            catalog_revision=snapshot.catalog_revision,
            cursor_scope=scope,
        )

    def _build_page(
        self,
        children: list[SessionCatalogNodeDTO],
        *,
        parent_node_id: str | None,
        offset: int,
        limit: int | None,
        revision: str,
        catalog_revision: int,
        cursor_scope: str | None,
    ) -> SessionCatalogPageDTO:
        """构造目录分页页；limit 为 None 表示返回 offset 之后的全部节点。"""
        page = children[offset:] if limit is None else children[offset : offset + limit]
        next_offset = offset + len(page)
        return SessionCatalogPageDTO(
            revision=revision,
            catalog_revision=catalog_revision,
            parent_node_id=parent_node_id,
            items=page,
            cursor=(
                self._encode_cursor(
                    next_offset, revision, catalog_revision, cursor_scope
                )
                if next_offset < len(children)
                else None
            ),
            total=len(children),
        )

    async def breadcrumb(self, node_id: str) -> SessionCatalogBreadcrumbDTO:
        snapshot = await self._snapshot()
        node = snapshot.nodes_by_id.get(node_id)
        if node is None:
            raise KeyError(f"会话目录节点不存在: {node_id}")
        return SessionCatalogBreadcrumbDTO(
            revision=snapshot.revision,
            catalog_revision=snapshot.catalog_revision,
            items=self._breadcrumb_items(node, snapshot.nodes_by_id),
        )

    async def search(
        self,
        *,
        query: str,
        limit: int,
        cursor: str | None,
    ) -> SessionCatalogSearchResultsDTO:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            raise ValueError("会话目录搜索词不能为空")
        snapshot = await self._snapshot()
        # 搜索的 cursor 作用域是查询词：把目录分页或其它查询词的 cursor 拿来翻本
        # 次搜索，会让 offset 落到错误匹配集的中间，静默跳项。
        scope = f"search\n{normalized_query}"
        matches: list[SessionCatalogNodeDTO] = []
        for node in snapshot.nodes:
            physical_path = snapshot.search_paths.get(node.node_id, "")
            if (
                normalized_query not in node.name.casefold()
                and normalized_query not in node.node_id.casefold()
                and normalized_query not in physical_path.casefold()
            ):
                continue
            matches.append(node)
        matches.sort(
            key=lambda item: (
                item.name.casefold(),
                snapshot.search_paths.get(item.node_id, "").casefold(),
                item.node_id,
            )
        )
        offset = self._decode_cursor(
            cursor,
            snapshot.revision,
            snapshot.catalog_revision,
            scope,
            collection_size=len(matches),
        )
        page_nodes = matches[offset : offset + limit]
        results: list[SessionCatalogSearchResultDTO] = []
        for node in page_nodes:
            breadcrumb = self._breadcrumb_items(node, snapshot.nodes_by_id)
            display_path = "/".join(item.name for item in breadcrumb)
            results.append(
                SessionCatalogSearchResultDTO(
                    node=node,
                    breadcrumb=breadcrumb,
                    relative_path=display_path,
                )
            )
        next_offset = offset + len(results)
        return SessionCatalogSearchResultsDTO(
            revision=snapshot.revision,
            catalog_revision=snapshot.catalog_revision,
            items=results,
            cursor=(
                self._encode_cursor(
                    next_offset,
                    snapshot.revision,
                    snapshot.catalog_revision,
                    scope,
                )
                if next_offset < len(matches)
                else None
            ),
            total=len(matches),
        )

    async def export_index(self) -> SessionCatalogExportDTO:
        snapshot = await self._snapshot()
        return SessionCatalogExportDTO(
            revision=snapshot.revision,
            catalog_revision=snapshot.catalog_revision,
            items=snapshot.nodes,
        )

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

    async def _snapshot(
        self,
        *,
        force: bool = False,
    ) -> _SessionCatalogReadSnapshot:
        current_revision = self._path_resolver.revision
        if (
            not force
            and self._cached_snapshot is not None
            and self._cached_snapshot.catalog_revision == current_revision
        ):
            # provider=None 的含义是当前配置默认值，缓存不能把它冻结成旧 provider。
            replacements = {
                node.node_id: self._session_service.project_catalog_session(node)
                for node in self._cached_snapshot.default_provider_nodes
            }
            if any(
                self._cached_snapshot.nodes_by_id[node_id].session != session
                for node_id, session in replacements.items()
            ):
                nodes = [
                    node.model_copy(update={"session": replacements[node.node_id]})
                    if node.node_id in replacements else node
                    for node in self._cached_snapshot.nodes
                ]
                self._cached_snapshot = replace(
                    self._cached_snapshot, nodes=nodes,
                    nodes_by_id={**self._cached_snapshot.nodes_by_id, **{node.node_id: node for node in nodes}},
                    revision=self._revision(nodes),
                )
            return self._cached_snapshot

        catalog_revision, all_physical_nodes = self._path_resolver.catalog_snapshot()
        physical_nodes = self._visible_catalog_nodes(all_physical_nodes)
        visible_parent_ids_with_children = {
            node.parent_node_id for node in physical_nodes if node.parent_node_id is not None
        }
        nodes = [
            self._to_catalog_node(node, visible_parent_ids_with_children)
            for node in physical_nodes
        ]
        nodes_by_id = {node.node_id: node for node in nodes}
        all_nodes_by_id = dict(nodes_by_id)
        all_nodes_by_id.update(
            {
                physical_node.node_id: self._to_pending_catalog_node(
                    physical_node, visible_parent_ids_with_children
                )
                for physical_node in all_physical_nodes
                if physical_node.node_id not in nodes_by_id
            }
        )
        self._validate_parent_graph(
            list(all_nodes_by_id.values()), all_nodes_by_id
        )
        revision = self._revision(nodes)
        snapshot = _SessionCatalogReadSnapshot(
            catalog_revision=catalog_revision,
            revision=revision,
            nodes=nodes,
            nodes_by_id=all_nodes_by_id,
            default_provider_nodes=[
                node for node in physical_nodes
                if isinstance(node, SessionCatalogSessionProjection) and node.current_provider_id is None
            ],
            search_paths={
                node.node_id: node.storage_relative_path
                for node in physical_nodes
                if isinstance(node, SessionCatalogSessionProjection)
            },
        )
        self._cached_snapshot = snapshot
        return snapshot

    @staticmethod
    def _visible_catalog_nodes(
        nodes: list[SessionCatalogNodeProjection],
    ) -> list[SessionCatalogNodeProjection]:
        """沿 active 父链枚举节点，隐藏 deleting 节点及其子树。"""
        children_by_parent: dict[str | None, list[SessionCatalogNodeProjection]] = {}
        for node in nodes:
            children_by_parent.setdefault(node.parent_node_id, []).append(node)

        visible: list[SessionCatalogNodeProjection] = []
        frontier: list[str | None] = [None]
        while frontier:
            parent_node_id = frontier.pop()
            for node in children_by_parent.get(parent_node_id, []):
                if node.state != "active":
                    continue
                visible.append(node)
                frontier.append(node.node_id)
        return sorted(visible, key=lambda node: node.node_id)

    def _to_catalog_node(
        self,
        node: SessionCatalogNodeProjection,
        visible_parent_ids_with_children: set[str],
    ) -> SessionCatalogNodeDTO:
        session = (
            self._session_service.project_catalog_session(node)
            if isinstance(node, SessionCatalogSessionProjection) else None
        )
        session_projection = (
            node if isinstance(node, SessionCatalogSessionProjection) else None
        )
        return SessionCatalogNodeDTO(
            node_id=node.node_id,
            kind=node.kind,
            name=session.title if session is not None else node.name,
            parent_node_id=node.parent_node_id,
            session_id=node.node_id if node.kind == "session" else None,
            folder_id=node.node_id if node.kind == "folder" else None,
            has_children=node.node_id in visible_parent_ids_with_children,
            node_revision=node.node_revision,
            state=node.state,
            created_at=(
                session_projection.created_at if session_projection is not None else None
            ),
            updated_at=(
                session_projection.updated_at if session_projection is not None else None
            ),
            session=session,
        )

    def _to_pending_catalog_node(
        self,
        node: SessionCatalogNodeProjection,
        visible_parent_ids_with_children: set[str],
    ) -> SessionCatalogNodeDTO:
        """把未进入 active 树的节点投影成可解析 breadcrumb 节点。

        目标可能处于 deleting，也可能位于 deleting 祖先之下。它们从普通目录
        枚举隐藏，且 session 元数据不可按正常路径读取，因此这里只给出节点身份
        与权威 node revision，不伪造 session 元数据。
        """
        session_projection = (
            node if isinstance(node, SessionCatalogSessionProjection) else None
        )
        return SessionCatalogNodeDTO(
            node_id=node.node_id,
            kind=node.kind,
            name=node.name,
            parent_node_id=node.parent_node_id,
            session_id=node.node_id if node.kind == "session" else None,
            folder_id=node.node_id if node.kind == "folder" else None,
            has_children=node.node_id in visible_parent_ids_with_children,
            node_revision=node.node_revision,
            state=node.state,
            created_at=(
                session_projection.created_at if session_projection is not None else None
            ),
            updated_at=(
                session_projection.updated_at if session_projection is not None else None
            ),
            session=None,
        )

    @staticmethod
    def _validate_parent_graph(
        nodes: list[SessionCatalogNodeDTO],
        nodes_by_id: dict[str, SessionCatalogNodeDTO],
    ) -> None:
        validated: set[str] = set()
        for node in nodes:
            current: SessionCatalogNodeDTO | None = node
            chain: set[str] = set()
            while current is not None and current.node_id not in validated:
                if current.node_id in chain:
                    raise RuntimeError(
                        f"会话物理目录包含循环关系: {current.node_id}"
                    )
                chain.add(current.node_id)
                if current.parent_node_id is None:
                    current = None
                    continue
                parent = nodes_by_id.get(current.parent_node_id)
                if parent is None:
                    raise RuntimeError(
                        "会话物理目录父节点不存在: "
                        f"node_id={current.node_id}, "
                        f"parent={current.parent_node_id}"
                    )
                current = parent
            validated.update(chain)

    @staticmethod
    def _sorted_children(
        nodes: list[SessionCatalogNodeDTO],
        parent_node_id: str | None,
    ) -> list[SessionCatalogNodeDTO]:
        children = [node for node in nodes if node.parent_node_id == parent_node_id]
        children.sort(
            key=lambda node: (node.kind != "folder", node.name.casefold(), node.node_id)
        )
        return children

    @staticmethod
    def _breadcrumb_items(
        node: SessionCatalogNodeDTO,
        nodes_by_id: dict[str, SessionCatalogNodeDTO],
    ) -> list[SessionCatalogNodeDTO]:
        items: list[SessionCatalogNodeDTO] = []
        visited: set[str] = set()
        current: SessionCatalogNodeDTO | None = node
        while current is not None:
            if current.node_id in visited:
                raise RuntimeError(f"会话物理目录包含循环关系: {current.node_id}")
            visited.add(current.node_id)
            items.append(current)
            if current.parent_node_id is None:
                current = None
                continue
            parent = nodes_by_id.get(current.parent_node_id)
            if parent is None:
                raise RuntimeError(
                    "会话物理目录父节点不存在: "
                    f"node_id={current.node_id}, parent={current.parent_node_id}"
                )
            current = parent
        items.reverse()
        return items

    @staticmethod
    def _revision(nodes: list[SessionCatalogNodeDTO]) -> str:
        encoded = json.dumps(
            [
                node.model_dump(mode="json")
                for node in sorted(nodes, key=lambda item: item.node_id)
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _encode_cursor(
        offset: int,
        revision: str,
        catalog_revision: int,
        scope: str | None,
    ) -> str:
        """签发 cursor：绑定 hash、navigation revision 与分页作用域。

        ``scope`` 是发牌时的分页上下文（list_children 为 ``parent_node_id``、
        search 为查询词）：同一 revision 下把 A 列表的 cursor 交给 B 列表，offset
        会落在 B 自身切片的中间，静默跳项且不报错。navigation revision 使其它节点变更
        也能使旧 cursor 失效；字符串 revision 仅是内容 hash，不充当 CAS 值。
        """
        payload = json.dumps(
            {
                "offset": offset,
                "revision": revision,
                "catalog_revision": catalog_revision,
                "scope": scope,
            }
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii")

    @staticmethod
    def _decode_cursor(
        cursor: str | None,
        revision: str,
        catalog_revision: int,
        scope: str | None,
        *,
        collection_size: int,
    ) -> int:
        if cursor is None:
            return 0
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
            )
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("会话目录 cursor 无效") from error
        if not isinstance(payload, dict):
            raise TypeError("会话目录 cursor 格式无效")
        if payload.get("revision") != revision:
            raise ValueError("会话目录已更新，请从第一页重新加载")
        if payload.get("catalog_revision") != catalog_revision:
            raise ValueError("会话目录已更新，请从第一页重新加载")
        if payload.get("scope") != scope:
            raise ValueError("会话目录 cursor 与当前列表不匹配，请从第一页重新加载")
        offset = payload.get("offset")
        if type(offset) is not int or offset < 0:
            raise ValueError("会话目录 cursor offset 无效")
        if offset >= collection_size:
            raise ValueError("会话目录 cursor offset 超出当前列表范围")
        return offset
