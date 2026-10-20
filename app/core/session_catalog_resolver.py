"""SessionCatalogPathResolver —— 新模型（SQLite catalog 权威）会话路径解析器。

以 ``SessionCatalogStore``（workspace
``navigation/session-catalog.sqlite``）为数据源，实现会话路径、导航和删除
所需的权威查询接口。

红线（模块边界，违反即失去本轮资格）：

- **唯一权威**：SQLite catalog 是唯一目录数据源；节点读取不依赖进程内缓存。
- **folder 无物理目录**：新模型 folder 是 SQLite-only 节点，使用独立的
  ``SessionCatalogFolderProjection``；``resolve_folder_dir`` 恒
  ``RuntimeError``。
- **逻辑移动不搬磁盘**：导航 operation executor 只改 SQLite
  ``parent_node_id``，不搬任何物理目录、不改写
  ``session.json``、不改 fork/delegation lineage/Session kind 或已封存
  context；执行中 Session 可移动（design.md §9）。
语义基准：design.md §9（统一两级 resolver；逻辑导航移动不搬磁盘；
folder 无物理目录；Gateway 只消费受控 catalog export）。

错误分类约定（沿用 ``session_catalog_store.py``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义
冲突/外部改动 fail closed。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeAlias

from app.core.session_catalog_store import (
    SessionCatalogNode,
    SessionCatalogStore,
    SessionCreationRecord,
    SubtreeDeleteRecord,
    validate_thread_id,
)
from app.core.session_catalog_store.contracts import (
    CatalogTransactionHook,
    SessionGenerationOriginDTO,
    SessionKind,
)
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore, ThreadCreationRecord
from app.core.session_lifecycle_gate import OperationHolderGate
from app.core.session_subtree_delete import (
    SessionSubtreeDeleteService,
    SubtreeDeleteResult,
)

__all__ = [
    "SessionCatalogFolderProjection",
    "SessionCatalogNodeProjection",
    "SessionCatalogPathResolver",
    "SessionCatalogSessionProjection",
    "SessionChildSummary",
]

# store.list_children 单页上限（BFS 全量投影的分页粒度）。
_LIST_CHILDREN_PAGE_LIMIT = 512


@dataclass(frozen=True, slots=True)
class SessionChildSummary:
    """直接逻辑子会话摘要（title 即 catalog display_name）。"""

    session_id: str
    title: str
    created_at: str


@dataclass(frozen=True, slots=True)
class SessionCatalogFolderProjection:
    """SQLite-only folder 节点投影。"""

    node_id: str
    kind: Literal["folder"]
    parent_node_id: str | None
    name: str
    node_revision: int
    # catalog 权威 state：active=已提交可见；deleting=逻辑删除中（读路径按语义
    # 将其从目录树暂时隐藏，但保留 state 让前端区分 pending 与 committed）。
    state: Literal["active", "deleting"] = "active"


@dataclass(frozen=True, slots=True)
class SessionCatalogSessionProjection:
    """带稳定物理 locator 的 session 节点投影。"""

    node_id: str
    kind: Literal["session"]
    parent_node_id: str | None
    name: str
    title_source: str
    node_revision: int
    created_at: datetime
    updated_at: datetime
    storage_relative_path: str
    workspace_id: str
    main_thread_id: str
    session_kind: SessionKind
    current_agent_id: str
    current_provider_id: str | None
    context_source_session_id: str | None
    generation_origin: SessionGenerationOriginDTO | None
    parent_session_id: str | None = None
    state: Literal["active", "deleting"] = "active"


SessionCatalogNodeProjection: TypeAlias = (
    SessionCatalogFolderProjection | SessionCatalogSessionProjection
)


class SessionCatalogPathResolver:
    """以 SessionCatalogStore（SQLite catalog）为数据源的会话路径解析器。

    节点与父链全部消费 store 的 SQLite 同一读快照；导航写入由 operation
    executor 持有，解析器只转发共享删除恢复生命周期。
    """

    def __init__(
        self,
        *,
        store: SessionCatalogStore,
        sessions_root: Path,
        workspace_id: str,
        delete_service: SessionSubtreeDeleteService,
    ) -> None:
        if not isinstance(store, SessionCatalogStore):
            raise TypeError(f"store 必须是 SessionCatalogStore: {store!r}")
        if not isinstance(sessions_root, Path):
            raise TypeError(f"sessions_root 必须是 Path: {sessions_root!r}")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        if not isinstance(delete_service, SessionSubtreeDeleteService):
            raise TypeError(
                f"delete_service 必须是 SessionSubtreeDeleteService: "
                f"{delete_service!r}"
            )
        self.sessions_root = sessions_root.expanduser().resolve()
        # resolver 与 catalog store 必须指向同一物理根（locator 解析、
        # staging/隔离区定位一致的前提），不一致 fail fast。
        if self.sessions_root != store.sessions_root:
            raise ValueError(
                "sessions_root 与 catalog store 的 sessions_root 不一致: "
                f"resolver={self.sessions_root}, store={store.sessions_root}"
            )
        self._store = store
        self._workspace_id = workspace_id
        self._delete_service = delete_service
        self._consistency_verified = False

    # ------------------------------------------------------------------
    # 公开装配属性
    # ------------------------------------------------------------------

    @property
    def catalog_store(self) -> SessionCatalogStore:
        """权威 catalog store 只读访问（8.5 装配面；不开放写事务）。"""
        return self._store

    @property
    def workspace_id(self) -> str:
        """返回本 resolver 绑定的 workspace_id（catalog 行归属键）。"""
        return self._workspace_id

    async def delete_subtree(
        self,
        *,
        idempotency_key: str,
        root_node_id: str,
        mark_transaction_hook: CatalogTransactionHook | None = None,
        finish_transaction_hook: CatalogTransactionHook | None = None,
    ) -> SubtreeDeleteResult:
        """以调用方给定 key 进入**共享**子树删除流（确定性恢复锚点）。

        直接转发到已装配、已绑定 drain 回调的共享 ``_delete_service``：同 key
        重入按既有 record 定点继续，不新造第二套删除实现。供 8.1-G 的导航
        mutation worker 以 operation_id 作稳定恢复 key。
        """
        return await self._delete_service.delete(
            idempotency_key=idempotency_key,
            root_node_id=root_node_id,
            mark_transaction_hook=mark_transaction_hook,
            finish_transaction_hook=finish_transaction_hook,
        )

    def bind_session_drain_callback(
        self,
        callback: Callable[[str], Awaitable[None]],
        *, holders: tuple[OperationHolderGate, ...] = (),
    ) -> None:
        """把单一运行时/资源复合 drain 回调绑定到共享子树删除流。"""
        self._delete_service.set_session_drain_callback(callback, holders=holders)

    def bind_fork_retention_release_callback(
        self, callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """绑定同一删除流的 gate 外 fork retention 释放阶段。"""
        self._delete_service.set_fork_retention_release_callback(callback)

    def bind_fork_child_abort_callback(self, callback: Callable[[str, ThreadCreationRecord], Awaitable[bool]]) -> None:
        """把未发布 fork child 的定点清理交给原 fork owner。"""
        self._delete_service.set_fork_child_abort_callback(callback)

    # ------------------------------------------------------------------
    # 节点投影
    # ------------------------------------------------------------------

    def _project_node(
        self, node: SessionCatalogNode, parent_session_id: str | None = None
    ) -> SessionCatalogNodeProjection:
        """把 catalog 行投影为明确的 catalog 节点类型。

        ``name`` 只来自 catalog；folder 无物理目录或时间字段，session 的
        物理路径由受检 storage locator 解析。运行时需要路径时必须调用
        ``resolve_session_node``，不能从可空 ``path`` 字段猜测。
        """
        if node.kind == "folder":
            return SessionCatalogFolderProjection(
                node_id=node.node_id,
                kind=node.kind,
                parent_node_id=node.parent_node_id,
                name=node.display_name,
                node_revision=node.revision,
                state=node.state,
            )
        if node.storage_relative_locator is None:
            raise RuntimeError(
                "session 节点缺少 storage_relative_locator"
                f"（catalog 被外部改动，fail closed）: node_id={node.node_id}"
            )
        if node.created_at is None:
            raise RuntimeError(
                "session 节点缺少 created_at"
                f"（catalog 被外部改动，fail closed）: node_id={node.node_id}"
            )
        if node.title_source not in ("default", "user", "auto"):
            raise RuntimeError(
                "session 节点缺少合法 title_source"
                f"（catalog 被外部改动，fail closed）: node_id={node.node_id}, "
                f"title_source={node.title_source!r}"
            )
        self._store.resolve_session_locator(node.storage_relative_locator)
        created_at = datetime.fromisoformat(node.created_at)
        if node.updated_at is None or not node.current_agent_id or node.main_thread_id is None:
            raise RuntimeError(f"session 节点缺少权威业务字段: node_id={node.node_id}")
        if node.session_kind not in ("normal", "context_fork"):
            raise RuntimeError(f"session 节点业务 kind 非法: node_id={node.node_id}, kind={node.session_kind!r}")
        generation_origin = (
            SessionGenerationOriginDTO.model_validate_json(node.generation_origin_json)
            if node.generation_origin_json is not None else None
        )
        return SessionCatalogSessionProjection(
            node_id=node.node_id,
            kind=node.kind,
            parent_node_id=node.parent_node_id,
            name=node.display_name,
            title_source=node.title_source,
            node_revision=node.revision,
            created_at=created_at,
            updated_at=datetime.fromisoformat(node.updated_at),
            storage_relative_path=node.storage_relative_locator[len("sessions/"):],
            workspace_id=node.workspace_id,
            main_thread_id=node.main_thread_id,
            session_kind=node.session_kind,
            current_agent_id=node.current_agent_id,
            current_provider_id=node.current_provider_id,
            context_source_session_id=node.context_source_session_id,
            generation_origin=generation_origin,
            parent_session_id=parent_session_id,
            state=node.state,
        )

    # ------------------------------------------------------------------
    # 全量/子树节点聚合（store 无 list-all 公开方法，BFS 分页聚合）
    # ------------------------------------------------------------------

    def _ensure_consistency_verified(self) -> None:
        """首次读取前做一次全表一致性校验。"""
        if self._consistency_verified:
            return
        self._store.verify_workspace_consistency()
        self._consistency_verified = True

    def _list_all_catalog_nodes(
        self,
        *,
        include_deleting: bool = False,
    ) -> list[SessionCatalogNode]:
        """按 node_id 稳定序返回 catalog 全量节点（list_children BFS 分页）。

        ``include_deleting=False``（读路径默认）时按 design.md §9 语义把
        ``deleting`` 子树暂时从目录树隐藏：单一节点进入 deleting 不再拖垮
        整棵目录的读取。删除恢复链路如需完整视图显式传 ``True``。
        """
        self._ensure_consistency_verified()
        nodes: dict[str, SessionCatalogNode] = {}
        frontier: list[str | None] = [None]
        while frontier:
            parent_node_id = frontier.pop()
            cursor: str | None = None
            while True:
                page, next_cursor, has_more = self._store.list_children(
                    parent_node_id,
                    limit=_LIST_CHILDREN_PAGE_LIMIT,
                    cursor=cursor,
                )
                for node in page:
                    if not include_deleting and node.state != "active":
                        continue
                    nodes[node.node_id] = node
                    frontier.append(node.node_id)
                if not has_more:
                    break
                cursor = next_cursor
        return [nodes[node_id] for node_id in sorted(nodes)]

    @staticmethod
    def _nearest_session_ancestor_from_map(
        parent_node_id: str | None,
        nodes_by_id: dict[str, SessionCatalogNode],
    ) -> str | None:
        """按 catalog 父链计算最近 session 祖先。

        传入 session 直接返回自身，folder 沿父链向上查找。
        """
        current_id = parent_node_id
        visited: set[str] = set()
        while current_id is not None:
            if current_id in visited:
                raise RuntimeError(f"会话目录包含循环: {current_id}")
            visited.add(current_id)
            node = nodes_by_id.get(current_id)
            if node is None:
                raise RuntimeError(f"物理会话节点父节点不存在: {current_id}")
            if node.kind == "session":
                return node.node_id
            current_id = node.parent_node_id
        return None

    # ------------------------------------------------------------------
    # 初始化与缓存适配
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """校验 catalog 全表一致性（fail closed）。

        store 已构造即视为可读；启动装配仍显式调用本方法，确保工作区
        catalog 在业务服务接管前通过完整性校验。
        """
        self._store.verify_workspace_consistency()
        _, nodes = self._store.read_nodes_snapshot()
        for node in nodes:
            if node.kind != "session" or node.state != "active":
                continue
            directory = self._store.resolve_session_locator(node.storage_relative_locator)
            manifest = json.loads((directory / "session.json").read_text(encoding="utf-8"))
            expected = {
                "session_id": node.node_id,
                "workspace_id": node.workspace_id,
                "created_at": node.created_at,
            }
            if manifest != expected or node.workspace_id != self._workspace_id:
                raise RuntimeError(
                    "会话与当前工作区后端标识不一致或身份 envelope 损坏: "
                    f"session_id={node.node_id}, manifest={manifest!r}, "
                    f"catalog_identity={expected!r}, backend_workspace_id={self._workspace_id!r}"
                )
        self._consistency_verified = True

    def list_nodes(self) -> list[SessionCatalogNodeProjection]:
        """返回 SQLite catalog 全量节点投影（按 node_id 稳定排序）。"""
        return [
            self._project_node(node)
            for node in self._list_all_catalog_nodes()
        ]

    def catalog_snapshot(
        self,
    ) -> tuple[int, list[SessionCatalogNodeProjection]]:
        """在同一 catalog 读快照中返回 navigation revision 与节点投影。"""
        self._ensure_consistency_verified()
        navigation_revision, nodes = self._store.read_nodes_snapshot()
        by_id = {node.node_id: node for node in nodes}
        return navigation_revision, [
            self._project_node(
                node, self._nearest_session_ancestor_from_map(node.parent_node_id, by_id)
            ) for node in nodes
        ]

    def session_list_page(
        self, *, skip: int, limit: int, cursor: str | None
    ) -> tuple[list[SessionCatalogSessionProjection], int, str | None, bool]:
        """索引分页与完整 DTO 投影均只消费同一 catalog 读事务。"""
        self._ensure_consistency_verified()
        nodes, parents, total, next_cursor, has_more = self._store.read_session_list_page(
            skip=skip, limit=limit, cursor=cursor
        )
        projected = [self._project_node(node, parents[node.node_id]) for node in nodes]
        sessions: list[SessionCatalogSessionProjection] = []
        for node in projected:
            if not isinstance(node, SessionCatalogSessionProjection):
                raise RuntimeError(f"会话列表返回非 session 节点: {node.node_id}")  # noqa: TRY004 - catalog 行损坏。
            sessions.append(node)
        return sessions, total, next_cursor, has_more

    # ------------------------------------------------------------------
    # 读方法
    # ------------------------------------------------------------------

    def get_node(self, node_id: str) -> SessionCatalogNodeProjection:
        """返回节点投影；不存在抛 KeyError。"""
        self._ensure_consistency_verified()
        node, parent_session_id = self._store.read_node_with_session_parent(node_id)
        return self._project_node(node, parent_session_id)

    def resolve_session_node(self, session_id: str) -> Path:
        """按稳定会话 ID 返回日期桶中的绝对会话目录（含存在性校验）。"""
        self._ensure_consistency_verified()
        node = self._store.get_node(session_id)
        if node.kind != "session":
            raise RuntimeError(f"节点不是会话: node_id={session_id}")
        if node.storage_relative_locator is None:
            raise RuntimeError(
                "session 节点缺少 storage_relative_locator"
                f"（catalog 被外部改动，fail closed）: node_id={session_id}"
            )
        path = self._store.resolve_session_locator(node.storage_relative_locator)
        if not path.is_dir() or path.is_symlink():
            raise RuntimeError(
                "会话物理目录缺失或不是普通目录，拒绝解析（fail closed）: "
                f"session_id={session_id}, path={path}"
            )
        return path

    def resolve_prepared_session_node(
        self,
        record: SessionCreationRecord,
    ) -> Path:
        """按同一 durable preparing record 解析尚未发布的精确 Session 目录。"""
        if not isinstance(record, SessionCreationRecord):
            raise TypeError(
                f"record 必须是 SessionCreationRecord: {record!r}"
            )
        self._ensure_consistency_verified()
        stored = self._store.get_creation_record(
            record.session_creation_idempotency_key
        )
        if stored != record:
            raise RuntimeError(
                "prepared SessionCreationRecord 与 durable row 不一致，拒绝解析: "
                f"key={record.session_creation_idempotency_key!r}"
            )
        if record.workspace_id != self._workspace_id:
            raise RuntimeError(
                "prepared record workspace 不属于当前 resolver: "
                f"record={record.workspace_id!r}, resolver={self._workspace_id!r}"
            )
        if record.state != "preparing":
            raise RuntimeError(
                "prepared Session 解析要求 state=preparing: "
                f"session_id={record.session_id}, state={record.state}"
            )
        if record.purpose not in ("normal", "context_fork"):
            raise RuntimeError(
                "prepared record purpose 非法（fail closed）: "
                f"purpose={record.purpose!r}"
            )
        staging = self.sessions_root / ".staging" / record.session_id
        target = self._store.resolve_session_locator(
            record.storage_relative_locator
        )
        staging_exists = staging.exists() or staging.is_symlink()
        target_exists = target.exists() or target.is_symlink()
        if staging_exists == target_exists:
            raise RuntimeError(
                "prepared record 必须且只能定位到 staging 或 frozen locator: "
                f"session_id={record.session_id}, staging={staging}, target={target}"
            )
        path = staging if staging_exists else target
        if not path.is_dir() or path.is_symlink():
            raise RuntimeError(
                "prepared Session 物理目录缺失或不是普通目录: "
                f"session_id={record.session_id}, path={path}"
            )
        return path

    def resolve_prepared_thread_node(
        self,
        record: SessionCreationRecord,
        thread_id: str,
    ) -> Path:
        """prepared target 只含 main thread，拒绝解析任何尚未发布的 child。"""
        path = self.resolve_prepared_session_node(record)
        validate_thread_id(thread_id)
        if thread_id != record.main_thread_id:
            raise KeyError(
                "prepared Session 尚无目标 child thread: "
                f"session_id={record.session_id}, thread_id={thread_id!r}"
            )
        thread_path = path / "threads" / record.main_thread_id
        if (path / "threads").is_symlink() or thread_path.is_symlink() or not thread_path.is_dir():
            raise RuntimeError(
                "prepared main thread 物理目录缺失或不是普通目录: "
                f"session_id={record.session_id}, thread_id={thread_id}, path={thread_path}"
            )
        return thread_path

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path:
        """按受检 SessionThread 解析线程资源目录。

        - main thread 使用 workspace catalog 冻结的 ``main_thread_id``，
          物理节点为 ``threads/{main_thread_id}``，不接受线程别名；
        - 非 main thread 的可见性和物理 locator 由 owner Session 的
          ``session-control.sqlite`` 共同校验：先读取已发布
          ``thread_catalog`` child row，再读取同一发布事务冻结的
          ``thread_creation_records.final_relative_locator``；不扫描
          ``threads/``，也不从日期桶猜测路径。
        """
        validate_thread_id(thread_id)
        session_dir = self.resolve_session_node(session_id)
        node = self._store.get_node(session_id)
        control_path = session_dir / CONTROL_DATABASE_NAME
        if not control_path.is_file() or control_path.is_symlink():
            raise RuntimeError(
                "Session control 数据库缺失或不是普通文件，拒绝解析 "
                f"thread（fail closed）: session_id={session_id}, "
                f"thread_id={thread_id}, control_path={control_path}"
            )
        control_store = SessionControlStore(control_path, read_only=True)
        try:
            if thread_id == node.main_thread_id:
                control_store.verify_matches_catalog_main_thread(thread_id)
                locator = f"threads/{thread_id}"
            else:
                locator = control_store.get_published_child_thread_locator(thread_id)
        finally:
            control_store.close()
        thread_path = session_dir / locator
        if (session_dir / "threads").is_symlink() or not thread_path.is_dir() or thread_path.is_symlink():
            raise RuntimeError(
                "已发布 thread 的冻结 locator 缺失或不是普通目录，"
                f"拒绝解析（fail closed）: session_id={session_id}, "
                f"thread_id={thread_id}, path={thread_path}"
            )
        return thread_path

    def resolve_session_node_for_runtime(self, session_id: str) -> Path:
        """运行时解析会话目录，统一走 SQLite catalog 路径校验。"""
        return self.resolve_session_node(session_id)

    def main_thread_id(self, session_id: str) -> str:
        """返回 Session 的唯一权威 main thread id（catalog 冻结指针）。

        ``(session_id, thread_id)`` 定位中的 thread 半必须来自 catalog，不能由
        调用方用 session id 冒充；节点不是 session、缺 main_thread_id 或
        catalog 被外部改动时 fail closed。
        """
        self._ensure_consistency_verified()
        node = self._store.get_node(session_id)
        if node.kind != "session" or node.main_thread_id is None:
            raise RuntimeError(
                "main thread 解析要求 session 节点且 catalog 已冻结 "
                f"main_thread_id（fail closed）: session_id={session_id!r}, "
                f"kind={node.kind!r}, main_thread_id={node.main_thread_id!r}"
            )
        return node.main_thread_id

    def resolve_folder_dir(self, folder_id: str) -> Path:
        """恒 RuntimeError：新模型 folder 无物理目录（design.md §9）。

        节点不存在抛 KeyError；存在的 folder 一律拒绝物理路径解析。
        """
        self._store.get_node(folder_id)
        raise RuntimeError(
            "folder 无物理目录（新模型 folder 是 SQLite-only 节点，"
            f"design.md §9）: folder_id={folder_id}"
        )

    def relative_path(self, node_id: str) -> str:
        """返回 session 的 storage locator 相对 sessions_root 的 POSIX 路径。

        仅 session 节点具有 storage locator；folder 无物理路径。
        """
        node = self._store.get_node(node_id)
        if node.kind != "session" or node.storage_relative_locator is None:
            raise RuntimeError(
                "folder 无物理目录，relative_path 仅支持 session 节点"
                f"（新模型语义）: node_id={node_id}, kind={node.kind}"
            )
        return node.storage_relative_locator[len("sessions/"):]

    def workspace_relative_path(self, node_id: str) -> str:
        """返回 session 目录相对 workspace 根的 POSIX 路径。

        workspace 根按标准布局 ``<workspace>/.boxteam/sessions`` 从
        sessions_root 上溯两级取得；布局不符 fail closed。
        """
        relative = self.relative_path(node_id)
        workspace_root = self.sessions_root.parent.parent
        try:
            prefix = self.sessions_root.relative_to(workspace_root).as_posix()
        except ValueError as error:
            raise RuntimeError(
                "sessions_root 不在标准 .boxteam 布局下，无法计算 workspace "
                f"相对路径: sessions_root={self.sessions_root}"
            ) from error
        return f"{prefix}/{relative}"

    def child_nodes(self, node_id: str) -> list[SessionCatalogNodeProjection]:
        """返回直接子节点投影（store.list_children 全量分页，稳定序）。

        节点不存在抛 KeyError；目录结构异常直接 fail closed。``deleting``
        子节点按 §9 语义暂时隐藏（与 :meth:`list_nodes` 一致）。
        """
        self._ensure_consistency_verified()
        items: list[SessionCatalogNodeProjection] = []
        cursor: str | None = None
        while True:
            page, next_cursor, has_more = self._store.list_children(
                node_id,
                limit=_LIST_CHILDREN_PAGE_LIMIT,
                cursor=cursor,
            )
            items.extend(
                self._project_node(node)
                for node in page
                if node.state == "active"
            )
            if not has_more:
                break
            cursor = next_cursor
        return items

    def child_session_summary(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> tuple[int, list[SessionChildSummary], bool]:
        """返回直接逻辑子会话摘要。

        「直接逻辑子会话」按最近 session 祖先归属：目标 session 的全部
        session 节点（含经 folder 间接挂载者）。items 元素为
        :class:`SessionChildSummary`（id/title/created_at，title 即
        catalog display_name）。
        """
        if limit < 1:
            raise ValueError("子会话摘要 limit 必须大于 0")
        node = self._store.get_node(session_id)
        if node.kind != "session":
            raise KeyError(f"物理会话节点不存在: {session_id}")
        all_nodes = self._list_all_catalog_nodes(include_deleting=True)
        nodes_by_id = {item.node_id: item for item in all_nodes}
        summaries: list[SessionChildSummary] = []
        for candidate in all_nodes:
            if candidate.kind != "session":
                continue
            # deleting 是已逻辑删除的 pending 状态，不能作为「可见子会话」计数。
            if candidate.state != "active":
                continue
            if (
                self._nearest_session_ancestor_from_map(
                    candidate.parent_node_id,
                    nodes_by_id,
                )
                != session_id
            ):
                continue
            summaries.append(
                SessionChildSummary(
                    session_id=candidate.node_id,
                    title=candidate.display_name,
                    created_at=candidate.created_at or "",
                )
            )
        return len(summaries), summaries[:limit], len(summaries) > limit

    def descendant_session_ids(
        self,
        node_id: str,
        *,
        include_self: bool = False,
    ) -> list[str]:
        """递归 CTE 返回全部后代 session ID（按 node_id 排序）。"""
        node = self._store.get_node(node_id)
        ids = self._store.descendant_session_ids(node_id)
        if include_self and node.kind == "session":
            ids = sorted([*ids, node_id])
        return ids

    def nearest_session_ancestor(self, node_id: str | None) -> str | None:
        """返回最近 session 祖先 ID（包含传入节点本身）。

        传入节点是 session 时直接返回它；否则沿父链向上找第一个
        session。节点缺失转换为 RuntimeError，并保留节点 ID 诊断信息。
        """
        if node_id is None:
            return None
        self._ensure_consistency_verified()
        try:
            node = self._store.get_node(node_id)
        except KeyError as error:
            raise RuntimeError(f"物理会话节点不存在: {node_id}") from error
        if node.kind == "session":
            return node.node_id
        return self._store.nearest_session_ancestor(node_id)

    def breadcrumb(self, node_id: str) -> list[SessionCatalogNodeProjection]:
        """返回从根到该节点（含自身）的节点投影链。

        每个投影带 catalog ``state``（active/deleting），调用方据此区分
        confirmed 与 pending；本方法不做隐藏过滤，保持面包屑对目标节点的
        可解析性，物理目录缺失（deleting 已隔离）不再 fail closed。
        """
        self._ensure_consistency_verified()
        return [
            self._project_node(node) for node in self._store.breadcrumb(node_id)
        ]

    # ------------------------------------------------------------------
    # Catalog 变更版本
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        """由真实 nodes 行变化推进的单调导航 revision，用于目录缓存失效。"""
        return self._store.current_navigation_revision()

    # ------------------------------------------------------------------
    def resolve_operation_replay_node(self, session_id: str, *, now: datetime) -> Path:
        """仅供独立 operation owner 核原请求后只读恢复，不开放普通 resolver。"""
        return self._delete_service.resolve_operation_replay_node(session_id, now=now)

    async def collect_expired_operation_replays(self, *, now: datetime) -> datetime | None:
        return await self._delete_service.collect_expired_operation_replays(now=now)

    def pending_subtree_deletes(self) -> list[SubtreeDeleteRecord]:
        """列出本 workspace 待恢复的子树删除 record（启动恢复的权威依据）。"""
        return self._store.list_pending_subtree_delete_records(self._workspace_id)

    async def recover_pending_subtree_deletes(
        self,
        *,
        mark_transaction_hook: CatalogTransactionHook | None = None,
        finish_transaction_hook: CatalogTransactionHook | None = None,
    ) -> list[SubtreeDeleteResult]:
        """按 SQLite 权威 record 幂等恢复所有未终结的子树删除（崩溃恢复入口）。

        对每条 ``preparing``/``deleting``/``draining`` record 以**原 idempotency
        key** 重入共享删除流：drain 未完成则定点续跑，已 drain 未 finish 则补
        tombstone，全部已完成则幂等返回。不扫描磁盘、不吸收外部改动、不假回滚
        active；任一恢复失败即向上抛错并保留 record，由启动期日志如实暴露。
        """
        results: list[SubtreeDeleteResult] = []
        for record in self.pending_subtree_deletes():
            results.append(
                await self._delete_service.delete(
                    idempotency_key=record.subtree_delete_idempotency_key,
                    root_node_id=record.root_node_id,
                    mark_transaction_hook=mark_transaction_hook,
                    finish_transaction_hook=finish_transaction_hook,
                )
            )
        return results
