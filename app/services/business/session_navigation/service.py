from __future__ import annotations

import asyncio
import base64
import hashlib
import json

from app.abstractions.job_service import JobServiceProtocol
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_resolver import (
    SessionCatalogNodeProjection,
    SessionCatalogPathResolver,
    SessionCatalogSessionProjection,
)
from app.core.session_subtree_delete import SubtreeDeleteResult
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogBreadcrumbDTO,
    SessionCatalogExportDTO,
    SessionCatalogNodeDTO,
    SessionCatalogPageDTO,
    SessionCatalogSearchResultDTO,
    SessionCatalogSearchResultsDTO,
    SessionFolderCreateRequest,
    SessionFolderUpdateRequest,
)
from app.schemas.internal_v2.session_navigation.operations import (
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
        self._cached_nodes: list[SessionCatalogNodeDTO] | None = None
        self._cached_revision: str | None = None
        self._cached_physical_revision: int | None = None
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
    ) -> SubtreeDeleteResult:
        """以确定性 key 进入共享子树删除流，保留既有 admission/预检语义。

        与删除前既有行为一致：先取得本次操作的 session 集合，在 JobService 删除
        admission 下做后台任务预检，再执行共享删除流（含运行时 drain 回调）。
        """
        frozen_session_ids = self._path_resolver.descendant_session_ids(root_node_id)

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
            )

        if self._job_service is None:
            return await run()
        return await self._job_service.run_sessions_delete_operation(
            frozen_session_ids,
            run,
        )

    def invalidate(self) -> None:
        self._cached_nodes = None
        self._cached_revision = None
        self._cached_physical_revision = None

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
        nodes, revision = await self._snapshot(force=True)
        roots = self._sorted_children(nodes, None)
        return SessionCatalogPageDTO(
            revision=revision,
            parent_node_id=None,
            items=roots[:500],
            cursor=None,
            total=len(roots),
        )

    async def list_children(
        self,
        *,
        parent_node_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> SessionCatalogPageDTO:
        nodes, revision = await self._snapshot()
        if parent_node_id is not None and not any(
            node.node_id == parent_node_id for node in nodes
        ):
            raise KeyError(f"会话目录节点不存在: {parent_node_id}")
        offset = self._decode_cursor(cursor, revision)
        children = self._sorted_children(nodes, parent_node_id)
        page = children[offset : offset + limit]
        next_offset = offset + len(page)
        return SessionCatalogPageDTO(
            revision=revision,
            parent_node_id=parent_node_id,
            items=page,
            cursor=(
                self._encode_cursor(next_offset, revision)
                if next_offset < len(children)
                else None
            ),
            total=len(children),
        )

    async def breadcrumb(self, node_id: str) -> SessionCatalogBreadcrumbDTO:
        nodes, revision = await self._snapshot()
        nodes_by_id = {node.node_id: node for node in nodes}
        node = nodes_by_id.get(node_id)
        if node is None:
            raise KeyError(f"会话目录节点不存在: {node_id}")
        return SessionCatalogBreadcrumbDTO(
            revision=revision,
            items=self._breadcrumb_items(node, nodes_by_id),
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
        nodes, revision = await self._snapshot()
        offset = self._decode_cursor(cursor, revision)
        nodes_by_id = {node.node_id: node for node in nodes}
        matches: list[SessionCatalogNodeDTO] = []
        for node in nodes:
            physical_path = node.storage_relative_path or ""
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
                (item.storage_relative_path or "").casefold(),
                item.node_id,
            )
        )
        page_nodes = matches[offset : offset + limit]
        results: list[SessionCatalogSearchResultDTO] = []
        for node in page_nodes:
            breadcrumb = self._breadcrumb_items(node, nodes_by_id)
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
            revision=revision,
            items=results,
            cursor=(
                self._encode_cursor(next_offset, revision)
                if next_offset < len(matches)
                else None
            ),
            total=len(matches),
        )

    async def export_index(self) -> SessionCatalogExportDTO:
        nodes, revision = await self._snapshot()
        return SessionCatalogExportDTO(revision=revision, items=nodes)

    async def create_folder(
        self,
        payload: SessionFolderCreateRequest,
    ) -> SessionCatalogBreadcrumbDTO:
        record = await self._submit(
            NavigationMutationIntentDTO(
                client_operation_id=create_prefixed_id("op"),
                client_sequence=1,
                kind="create_folder",
                base_catalog_revision=self._catalog_revision(),
                name=payload.name,
                parent_node_id=payload.parent_folder_id,
            )
        )
        self.invalidate()
        return await self.breadcrumb(_committed_node_id(record))

    async def update_folder(
        self,
        folder_id: str,
        payload: SessionFolderUpdateRequest,
    ) -> SessionCatalogBreadcrumbDTO:
        if self._operations_service.node_kind(folder_id) != "folder":
            raise KeyError(f"会话文件夹不存在: {folder_id}")
        intents = self._folder_update_intents(folder_id, payload)
        if not intents:
            self.invalidate()
            return await self.breadcrumb(folder_id)
        await self._submit_batch(intents)
        self.invalidate()
        return await self.breadcrumb(folder_id)

    async def assign_session(
        self,
        session_id: str,
        folder_id: str | None,
    ) -> SessionCatalogBreadcrumbDTO:
        """把会话分配进文件夹；目标必须是 folder（保持该端点的既有契约）。"""
        if folder_id is not None and self.operations.node_kind(folder_id) != "folder":
            raise ValueError(f"目标节点不是会话文件夹: {folder_id}")
        await self._submit_batch(
            [self._move_intent(session_id, folder_id, sequence=1)]
        )
        self.invalidate()
        return await self.breadcrumb(session_id)

    async def move_node(
        self,
        node_id: str,
        parent_node_id: str | None,
    ) -> SessionCatalogBreadcrumbDTO:
        """按目标节点类型移动会话或会话文件夹。"""
        await self._submit_batch(
            [self._move_intent(node_id, parent_node_id, sequence=1)]
        )
        self.invalidate()
        return await self.breadcrumb(node_id)

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
        return self.operations.snapshot().catalog_revision

    async def _submit(
        self,
        intent: NavigationMutationIntentDTO,
    ) -> NavigationMutationRecord:
        return await self.operations.submit_single(intent, self._scope())

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

    def _move_intent(
        self,
        node_id: str,
        parent_node_id: str | None,
        *,
        sequence: int,
    ) -> NavigationMutationIntentDTO:
        return NavigationMutationIntentDTO(
            client_operation_id=create_prefixed_id("op"),
            client_sequence=sequence,
            kind="move_node",
            base_catalog_revision=self._catalog_revision(),
            expected_revision=self.operations.node_revision(node_id),
            target_node_id=node_id,
            parent_node_id=parent_node_id,
        )

    def _folder_update_intents(
        self,
        folder_id: str,
        payload: SessionFolderUpdateRequest,
    ) -> list[NavigationMutationIntentDTO]:
        """把 folder 更新拆成 typed intent 序列（改名 + 移动可同批提交）。

        同 node 连续编辑通过 ``depends_on`` 建立依赖：执行时以后序 intent 引用
        前序已提交结果 revision 作 CAS 前置，因此不会因自身前一条命令推进了
        revision 而误判冲突。
        """
        intents: list[NavigationMutationIntentDTO] = []
        previous_id: str | None = None
        if payload.name is not None:
            rename_id = create_prefixed_id("op")
            intents.append(
                NavigationMutationIntentDTO(
                    client_operation_id=rename_id,
                    client_sequence=1,
                    kind="rename_node",
                    base_catalog_revision=self._catalog_revision(),
                    expected_revision=self.operations.node_revision(folder_id),
                    target_node_id=folder_id,
                    name=payload.name,
                )
            )
            previous_id = rename_id
        if "parent_folder_id" in payload.model_fields_set:
            move_id = create_prefixed_id("op")
            intents.append(
                NavigationMutationIntentDTO(
                    client_operation_id=move_id,
                    client_sequence=len(intents) + 1,
                    kind="move_node",
                    base_catalog_revision=self._catalog_revision(),
                    expected_revision=self.operations.node_revision(folder_id),
                    target_node_id=folder_id,
                    parent_node_id=payload.parent_folder_id,
                    depends_on=[previous_id] if previous_id else [],
                )
            )
        return intents

    async def delete_folder(
        self,
        folder_id: str,
        *,
        recursive: bool = False,
    ) -> None:
        """删除 folder：非递归要求为空，递归走共享子树删除 operation。"""
        if self._operations_service.node_kind(folder_id) != "folder":
            raise KeyError(f"会话文件夹不存在: {folder_id}")
        if not recursive:
            try:
                self._path_resolver.delete_folder(folder_id)
            except RuntimeError as error:
                raise ValueError(str(error)) from error
            self.invalidate()
            return
        await self._submit_batch(
            [
                NavigationMutationIntentDTO(
                    client_operation_id=create_prefixed_id("op"),
                    client_sequence=1,
                    kind="delete_folder",
                    base_catalog_revision=self._catalog_revision(),
                    target_node_id=folder_id,
                    recursive=True,
                )
            ]
        )
        self.invalidate()

    async def _snapshot(
        self,
        *,
        force: bool = False,
    ) -> tuple[list[SessionCatalogNodeDTO], str]:
        physical_revision = self._path_resolver.revision
        if (
            not force
            and self._cached_nodes is not None
            and self._cached_revision is not None
            and self._cached_physical_revision == physical_revision
        ):
            return self._cached_nodes, self._cached_revision
        physical_nodes = self._path_resolver.list_nodes()
        physical_revision = self._path_resolver.revision
        child_parent_ids = {
            node.parent_node_id
            for node in physical_nodes
            if node.parent_node_id is not None
        }
        session_metadata = await self._load_session_metadata(physical_nodes)
        nodes = [
            self._to_catalog_node(node, child_parent_ids, session_metadata)
            for node in physical_nodes
        ]
        nodes_by_id = {node.node_id: node for node in nodes}
        self._validate_parent_graph(nodes, nodes_by_id)
        revision = self._revision(nodes)
        self._cached_nodes = nodes
        self._cached_revision = revision
        self._cached_physical_revision = physical_revision
        return nodes, revision

    async def _load_session_metadata(
        self,
        physical_nodes: list[SessionCatalogNodeProjection],
    ) -> dict[str, SessionDTO]:
        session_nodes = [node for node in physical_nodes if node.kind == "session"]
        if not session_nodes:
            return {}
        sessions = await asyncio.gather(
            *(self._session_service.get(node.node_id) for node in session_nodes)
        )
        metadata: dict[str, SessionDTO] = {}
        for node, session in zip(session_nodes, sessions, strict=True):
            if session.session_id != node.node_id:
                raise RuntimeError(
                    "会话目录节点与 session manifest ID 不一致: "
                    f"node_id={node.node_id}, session_id={session.session_id}"
                )
            if session.session_id in metadata:
                raise RuntimeError(
                    f"会话目录返回重复会话元数据: session_id={session.session_id}"
                )
            if session.title != node.name:
                raise RuntimeError(
                    "会话目录节点名称与会话元数据标题不一致: "
                    f"session_id={node.node_id}, node_name={node.name}, "
                    f"session_title={session.title}"
                )
            metadata[session.session_id] = session
        return metadata

    def _to_catalog_node(
        self,
        node: SessionCatalogNodeProjection,
        child_parent_ids: set[str],
        session_metadata: dict[str, SessionDTO],
    ) -> SessionCatalogNodeDTO:
        session = session_metadata.get(node.node_id)
        if node.kind == "session" and session is None:
            raise RuntimeError(f"会话目录节点缺少会话元数据: session_id={node.node_id}")
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
            has_children=node.node_id in child_parent_ids,
            storage_relative_path=(
                session_projection.storage_relative_path
                if session_projection is not None
                else None
            ),
            created_at=(
                session_projection.created_at if session_projection is not None else None
            ),
            updated_at=(
                session_projection.updated_at if session_projection is not None else None
            ),
            session=session,
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
    def _encode_cursor(offset: int, revision: str) -> str:
        payload = json.dumps({"offset": offset, "revision": revision}).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii")

    @staticmethod
    def _decode_cursor(cursor: str | None, revision: str) -> int:
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
        offset = payload.get("offset")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("会话目录 cursor offset 无效")
        return offset
