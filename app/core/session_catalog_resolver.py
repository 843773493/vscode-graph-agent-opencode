"""SessionCatalogPathResolver —— 新模型（SQLite catalog 权威）会话路径解析器。

OpenSpec add-itemized-rollout-context 8.2-切片3a（R15）。以
``SessionCatalogStore``（workspace ``navigation/session-catalog.sqlite``）为
数据源，实现会话路径、导航和删除所需的权威查询接口。

红线（模块边界，违反即失去本轮资格）：

- **唯一权威**：SQLite catalog 是唯一目录数据源；节点读取不依赖进程内缓存。
- **folder 无物理目录**：新模型 folder 是 SQLite-only 节点——投影
  ``path=None``（``created_at``/``updated_at`` 同为 None，catalog 中 folder
  无时间字段，不伪造时间值）；``resolve_folder_dir`` 恒 ``RuntimeError``。
  旧调用方对 ``folder.path`` / ``resolve_folder_dir`` /
  ``relative_path(folder)`` 的使用须在切换轮逐一核对适配。
- **逻辑移动不搬磁盘**：``move_node``/``relocate_session``/
  ``relocate_folder_tree`` 只改 SQLite ``parent_node_id``，不搬任何物理
  目录、不改写 ``session.json``、不改 fork/delegation lineage/Session kind
  或已封存 context；执行中 Session 可移动（design.md §9）。
语义基准：design.md §9（统一两级 resolver；逻辑导航移动不搬磁盘；
folder 无物理目录；Gateway 只消费受控 catalog export）。

错误分类约定（沿用 ``session_catalog_store.py``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义
冲突/外部改动 fail closed。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import (
    SessionCatalogNode,
    SessionCatalogStore,
    validate_thread_id,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_subtree_delete import SessionSubtreeDeleteService
from app.core.session_tree.support import SessionPhysicalNode

__all__ = [
    "SessionCatalogPathResolver",
    "SessionChildSummary",
]

# store.list_children 单页上限（BFS 全量投影的分页粒度）。
_LIST_CHILDREN_PAGE_LIMIT = 512
_CONTROL_DATABASE_NAME = "session-control.sqlite"

@dataclass(frozen=True, slots=True)
class SessionChildSummary:
    """直接逻辑子会话摘要（title 即 catalog display_name）。"""

    session_id: str
    title: str
    created_at: str


class SessionCatalogPathResolver:
    """以 SessionCatalogStore（SQLite catalog）为数据源的会话路径解析器。

    与旧 ``SessionPathResolver`` 的 duck-typing 契约见模块 docstring 红线。
    进程内 ``threading.RLock`` 只保护自身实例表（待注册分配、子树删除
    key）；节点读取全部走 store 的 SQLite 直读，天然进程内新鲜。
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
        # staging/隔离区定位一致的前提），不一致 fail fast（对齐 R13/R14）。
        if self.sessions_root != store.sessions_root:
            raise ValueError(
                "sessions_root 与 catalog store 的 sessions_root 不一致: "
                f"resolver={self.sessions_root}, store={store.sessions_root}"
            )
        self._store = store
        self._workspace_id = workspace_id
        self._delete_service = delete_service
        self._lock = threading.RLock()
        self._subtree_delete_keys: dict[str, str] = {}
        self._consistency_verified = False

    # ------------------------------------------------------------------
    # 兼容属性
    # ------------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        """兼容属性名：新模型权威索引即 SQLite catalog 数据库文件。"""
        return self._store.database_path

    @property
    def catalog_store(self) -> SessionCatalogStore:
        """权威 catalog store 只读访问（8.5 装配面；不开放写事务）。"""
        return self._store

    def bind_session_drain_callback(
        self,
        callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """把运行时排空端口绑定到共享子树删除流。"""
        self._delete_service.set_session_drain_callback(callback)

    # ------------------------------------------------------------------
    # 节点投影
    # ------------------------------------------------------------------

    def _project_node(self, node: SessionCatalogNode) -> SessionPhysicalNode:
        """把 catalog 行投影为 SessionPhysicalNode。

        - ``name = display_name``（显示名只存 catalog）；
        - session 的 ``path`` 由 storage locator 解析；folder 无物理目录，
          ``path`` 投影为 ``None``——调用方不得对 folder 取 path（旧调用方
          在切换轮逐一核对）；
        - 新模型无节点级 ``updated_at``（revision 表达变更计数），投影
          ``updated_at = created_at`` 保持字段完整；folder 在 catalog 中
          无 ``created_at``，投影 ``None``（不伪造时间值）。
        """
        if node.kind == "folder":
            # SessionPhysicalNode 的 path/created_at/updated_at 注解为
            # Path/datetime，folder 投影按本模块红线携带 None（无运行时
            # 强制），与任务书 §2.1-B「folder.path 返回 None」决定一致。
            return SessionPhysicalNode(  # type: ignore[arg-type]
                node_id=node.node_id,
                kind=node.kind,
                path=None,  # type: ignore[arg-type]
                parent_node_id=node.parent_node_id,
                name=node.display_name,
                created_at=None,  # type: ignore[arg-type]
                updated_at=None,  # type: ignore[arg-type]
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
        path = self._store.resolve_session_locator(node.storage_relative_locator)
        created_at = datetime.fromisoformat(node.created_at)
        return SessionPhysicalNode(
            node_id=node.node_id,
            kind=node.kind,
            path=path,
            parent_node_id=node.parent_node_id,
            name=node.display_name,
            created_at=created_at,
            updated_at=created_at,
        )

    # ------------------------------------------------------------------
    # 全量/子树节点聚合（store 无 list-all 公开方法，BFS 分页聚合）
    # ------------------------------------------------------------------

    def _ensure_consistency_verified(self) -> None:
        """首次读取前做一次全表一致性校验（对齐旧 initialize 契约）。"""
        if self._consistency_verified:
            return
        self._store.verify_workspace_consistency()
        self._consistency_verified = True

    def _list_all_catalog_nodes(self) -> list[SessionCatalogNode]:
        """按 node_id 稳定序返回 catalog 全量节点（list_children BFS 分页）。"""
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
                    nodes[node.node_id] = node
                    frontier.append(node.node_id)
                if not has_more:
                    break
                cursor = next_cursor
        return [nodes[node_id] for node_id in sorted(nodes)]

    def _subtree_catalog_node_ids(self, node_id: str) -> set[str]:
        """递归返回子树全部节点 ID（含自身）。"""
        self._ensure_consistency_verified()
        result = {node_id}
        frontier = [node_id]
        while frontier:
            current_id = frontier.pop()
            cursor: str | None = None
            while True:
                page, next_cursor, has_more = self._store.list_children(
                    current_id,
                    limit=_LIST_CHILDREN_PAGE_LIMIT,
                    cursor=cursor,
                )
                for node in page:
                    if node.node_id in result:
                        raise RuntimeError(f"会话目录包含循环: {node.node_id}")
                    result.add(node.node_id)
                    frontier.append(node.node_id)
                if not has_more:
                    break
                cursor = next_cursor
        return result

    @staticmethod
    def _nearest_session_ancestor_from_map(
        parent_node_id: str | None,
        nodes_by_id: dict[str, SessionCatalogNode],
    ) -> str | None:
        """旧 ``nearest_session_ancestor_from_nodes`` 语义的纯内存实现。

        注意：语义包含传入节点本身（传入 session 直接返回它）；store 的
        同名方法从父链开始（不含自身），两者不同，不得混用。
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

        store 已构造即视为初始化完成；SQLite 直读模型下无加载态，本方法
        只保留旧接口契约（构造后调用一次）并做 fail-closed 校验。
        """
        with self._lock:
            self._store.verify_workspace_consistency()
            self._consistency_verified = True

    def invalidate(self) -> None:
        """no-op：SQLite 直读天然新鲜，保留旧签名兼容。"""
        return

    def list_nodes(self, *, refresh: bool = False) -> list[SessionPhysicalNode]:
        """catalog 全量节点投影（按 node_id 排序稳定序）。

        ``refresh`` 形参保留旧签名兼容；SQLite 直读下每次都读到最新提交，
        刷新与否无差异。
        """
        del refresh  # 签名兼容：直读模型无缓存可刷新。
        return [
            self._project_node(node)
            for node in self._list_all_catalog_nodes()
        ]

    def list_authoritative_nodes(self) -> list[SessionPhysicalNode]:
        """同 :meth:`list_nodes`：catalog 即唯一权威投影。"""
        return self.list_nodes()

    def refresh(self) -> list[SessionPhysicalNode]:
        """no-op 刷新后返回全量投影（旧签名兼容）。"""
        return self.list_nodes()

    # ------------------------------------------------------------------
    # 读方法
    # ------------------------------------------------------------------

    def get_node(self, node_id: str) -> SessionPhysicalNode:
        """返回节点投影；不存在抛 KeyError。"""
        self._ensure_consistency_verified()
        return self._project_node(self._store.get_node(node_id))

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

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path:
        """按受检 SessionThread 解析线程资源目录。

        - main thread 折叠保持（R3a）：``"main"`` / session 自身 ID /
          catalog ``main_thread_id`` 都解析到会话目录自身；
        - 非 main thread 的可见性和物理 locator 由 owner Session 的
          ``session-control.sqlite`` 共同校验：先读取已发布
          ``thread_catalog`` child row，再读取同一发布事务冻结的
          ``thread_creation_records.final_relative_locator``；不扫描
          ``threads/``，也不从日期桶猜测路径。
        """
        session_dir = self.resolve_session_node(session_id)
        node = self._store.get_node(session_id)
        if (
            not thread_id
            or thread_id == "main"
            or thread_id == session_id
            or thread_id == node.main_thread_id
        ):
            return session_dir
        # child Session 节点是 NodeDebug 的折叠 thread owner：父会话地址携带
        # 子会话 ID 时，直接返回子会话自身目录。该别名由 catalog 子树约束，
        # 不扫描磁盘，也不把任意字符串当作 thread_id 接受。
        try:
            child_node = self._store.get_node(thread_id)
        except KeyError:
            if thread_id.startswith("ses_"):
                raise KeyError(
                    "目标 child session 不存在: "
                    f"session_id={session_id}, thread_id={thread_id}"
                ) from None
            child_node = None
        if (
            child_node is not None
            and child_node.kind == "session"
        ):
            if child_node.parent_node_id != session_id:
                raise RuntimeError(
                    "thread 不属于目标 session: "
                    f"session_id={session_id}, thread_id={thread_id}"
                )
            return self.resolve_session_node(thread_id)
        validate_thread_id(thread_id)
        control_path = session_dir / _CONTROL_DATABASE_NAME
        if not control_path.is_file() or control_path.is_symlink():
            raise RuntimeError(
                "Session control 数据库缺失或不是普通文件，拒绝解析 child "
                f"thread（fail closed）: session_id={session_id}, "
                f"thread_id={thread_id}, control_path={control_path}"
            )
        control_store = SessionControlStore(control_path)
        try:
            locator = control_store.get_published_child_thread_locator(thread_id)
        finally:
            control_store.close()
        thread_path = session_dir / locator
        if not thread_path.is_dir() or thread_path.is_symlink():
            raise RuntimeError(
                "已发布 child thread 的冻结 locator 缺失或不是普通目录，"
                f"拒绝解析（fail closed）: session_id={session_id}, "
                f"thread_id={thread_id}, path={thread_path}"
            )
        return thread_path

    def resolve_session_node_for_runtime(self, session_id: str) -> Path:
        """运行时解析会话目录；语义同 :meth:`resolve_session_node`。

        旧语义「运行时解析不触发迁移」：新模型无迁移语义，SQLite 直读
        即最新权威，不做额外区分。
        """
        return self.resolve_session_node(session_id)

    def resolve_folder_dir(self, folder_id: str) -> Path:
        """恒 RuntimeError：新模型 folder 无物理目录（design.md §9）。

        节点不存在仍先抛 KeyError（保留「目标不存在」信号），存在则一律
        拒绝——旧调用方对 folder 物理目录的依赖须在切换轮适配。
        """
        self._store.get_node(folder_id)
        raise RuntimeError(
            "folder 无物理目录（新模型 folder 是 SQLite-only 节点，"
            f"design.md §9），旧调用方须在切换轮适配: folder_id={folder_id}"
        )

    def relative_path(self, node_id: str) -> str:
        """返回 session 的 storage locator 相对 sessions_root 的 POSIX 路径。

        新模型仅支持 session 节点（folder 无物理目录，旧调用方对 folder
        的 relative_path 使用须切换轮适配）。
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

    def child_nodes(self, node_id: str) -> list[SessionPhysicalNode]:
        """返回直接子节点投影（store.list_children 全量分页，稳定序）。

        节点不存在抛 KeyError（旧实现对缺失节点静默返回空列表，此处按
        fail-closed 收紧；调用方在切换轮核对）。
        """
        self._ensure_consistency_verified()
        items: list[SessionPhysicalNode] = []
        cursor: str | None = None
        while True:
            page, next_cursor, has_more = self._store.list_children(
                node_id,
                limit=_LIST_CHILDREN_PAGE_LIMIT,
                cursor=cursor,
            )
            items.extend(self._project_node(node) for node in page)
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
        """返回直接逻辑子会话摘要，保持旧 ``(count, items, has_more)`` 分页形态。

        「直接逻辑子会话」沿用旧语义：最近 session 祖先等于目标 session
        的全部 session 节点（含经 folder 间接挂载者）。items 元素升级为
        :class:`SessionChildSummary`（id/title/created_at，title 即
        catalog display_name）；旧调用方读取 id 的位置须在切换轮改为
        ``.session_id``。
        """
        if limit < 1:
            raise ValueError("子会话摘要 limit 必须大于 0")
        node = self._store.get_node(session_id)
        if node.kind != "session":
            raise KeyError(f"物理会话节点不存在: {session_id}")
        all_nodes = self._list_all_catalog_nodes()
        nodes_by_id = {item.node_id: item for item in all_nodes}
        summaries: list[SessionChildSummary] = []
        for candidate in all_nodes:
            if candidate.kind != "session":
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
        """返回最近 session 祖先 ID（旧语义：包含传入节点本身）。

        传入节点是 session 时直接返回它；否则沿父链向上找第一个
        session。节点缺失保持旧 ``RuntimeError`` 契约（store 层为
        KeyError，此处转换）。
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

    def breadcrumb(self, node_id: str) -> list[SessionPhysicalNode]:
        """返回从根到该节点（含自身）的节点投影链。"""
        self._ensure_consistency_verified()
        return [
            self._project_node(node) for node in self._store.breadcrumb(node_id)
        ]

    # ------------------------------------------------------------------
    # 兼容属性（旧接口面）
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        """变更敏感计数：``sum(所有节点 revision)``。

        与旧 index mtime 计数的语义差异：旧计数来自进程内物理树刷新
        次数；新计数直接聚合 catalog 行 revision（每次导航写 +1）。
        调用方仅用于缓存失效判定，SQLite 直读模型下该用途弱化。
        性能注记：每次读取做一次 BFS 分页聚合（不装配轮无热点；切换轮
        如需优化应由 store 提供聚合查询公开方法）。
        """
        return sum(node.revision for node in self._list_all_catalog_nodes())

    @property
    def physical_tree_error(self) -> str | None:
        """恒 None：新模型无后台物理树校验，resolve 时点校验（fail closed）。"""
        return None

    @property
    def authoritative_revision(self) -> int:
        """同 :attr:`revision`：catalog 直读下无「权威/物理」二分。"""
        return self.revision

    @property
    def legacy_inline_attachment_migration_record(self) -> dict[str, object]:
        """空 dict：旧 inline 附件迁移已完成，新工作区无此概念。"""
        return {}

    # ------------------------------------------------------------------
    # 导航写方法（SQLite-only，不动物理）
    # ------------------------------------------------------------------

    def update_node_name(self, node_id: str, name: str) -> SessionPhysicalNode:
        """重命名节点显示名（仅 SQLite；旧语义 rename 物理目录名，新模型
        显示名不参与路径，物理目录保持稳定）。"""
        return self._project_node(self._store.rename_node(node_id, name))

    def create_folder(
        self,
        *,
        name: str,
        parent_node_id: str | None,
    ) -> SessionPhysicalNode:
        """创建 folder 节点：软件分配 ID、无物理目录、无 manifest。

        folder 与 session 共用 ``ses_`` ID profile（R10 口径）；显示名只
        存 catalog。
        """
        # TODO(identifier): "thr" 同款约束——folder 复用 "ses" 前缀是
        # R10 确定的共用口径，见 session_catalog_store.create_folder。
        folder_id = create_prefixed_id("ses")
        node = self._store.create_folder(
            folder_id,
            self._workspace_id,
            parent_node_id,
            name,
        )
        return self._project_node(node)

    def move_node(
        self,
        *,
        node_id: str,
        parent_node_id: str | None,
        name: str | None = None,
    ) -> SessionPhysicalNode:
        """逻辑移动 folder（只改 parent_node_id，不搬磁盘）。

        ``name`` 形参保留旧签名兼容；新模型重命名走
        :meth:`update_node_name`——传入与当前 display_name 不同的 name
        直接拒绝（fail closed，防止切换期静默丢失重命名）。
        """
        node = self._store.get_node(node_id)
        if node.kind != "folder":
            raise ValueError(
                f"move_node 只允许移动会话文件夹，会话必须走 relocate_session: "
                f"{node_id}"
            )
        if name is not None and name != node.display_name:
            raise ValueError(
                "新模型 move_node 不承担重命名，显示名变更走 update_node_name: "
                f"node_id={node_id}, current={node.display_name!r}, "
                f"requested={name!r}"
            )
        return self._project_node(self._store.move_node(node_id, parent_node_id))

    def relocate_session(
        self,
        *,
        session_id: str,
        parent_node_id: str | None,
    ) -> SessionPhysicalNode:
        """逻辑移动 session（改 parent；不搬目录、不改 manifest）。

        新模型 ``parent_session_id`` 是 catalog 派生关系，不再改写
        session.json（旧签名的 ``manifest`` 形参整体移除，切换轮适配）；
        fork/delegation lineage 与 Session kind 不受逻辑移动影响。
        """
        node = self._store.get_node(session_id)
        if node.kind != "session":
            raise RuntimeError(f"节点不是会话: {session_id}")
        return self._project_node(self._store.move_node(session_id, parent_node_id))

    def relocate_folder_tree(
        self,
        *,
        folder_id: str,
        parent_node_id: str | None,
    ) -> SessionPhysicalNode:
        """逻辑移动 folder 子树（根节点改 parent，后代关系不变）。

        旧签名的 ``name``/``session_manifests`` 形参整体移除：新模型
        folder 无物理目录（无 rename 语义）、session 父关系由 catalog
        派生（无 manifest 改写）；显示名变更走 :meth:`update_node_name`。
        """
        folder = self._store.get_node(folder_id)
        if folder.kind != "folder":
            raise ValueError(f"节点不是会话文件夹: {folder_id}")
        return self._project_node(self._store.move_node(folder_id, parent_node_id))

    def expected_session_parents_after_folder_move(
        self,
        *,
        folder_id: str,
        parent_node_id: str | None,
    ) -> dict[str, str | None]:
        """计算 folder 子树移动后每个 session 应声明的最近 session 父会话。

        新模型派生语义（保持调用方契约）：移动后各 session 的
        ``parent_session_id`` = 新位置下最近 session 祖先——子树内部
        （沿子树父链向上）优先，否则为目标位置的外部最近 session 祖先。
        本方法只读 catalog，不产生任何写入。
        """
        folder = self._store.get_node(folder_id)
        if folder.kind != "folder":
            raise ValueError(f"节点不是会话文件夹: {folder_id}")
        if parent_node_id == folder_id:
            raise ValueError(f"节点不能移动到自身下: {folder_id}")
        subtree_ids = self._subtree_catalog_node_ids(folder_id)
        if parent_node_id is not None:
            if parent_node_id in subtree_ids:
                raise ValueError(
                    f"移动会形成目录循环: node_id={folder_id}, "
                    f"parent={parent_node_id}"
                )
            # 父节点必须存在（缺失 KeyError，对齐旧 get_node 行为）。
            self._store.get_node(parent_node_id)
        external_parent_session_id = self.nearest_session_ancestor(parent_node_id)
        nodes_by_id = {
            node.node_id: node for node in self._list_all_catalog_nodes()
        }
        expected: dict[str, str | None] = {}
        for node_id in sorted(subtree_ids):
            node = nodes_by_id[node_id]
            if node.kind != "session":
                continue
            ancestor_id = node.parent_node_id
            nearest_internal_session_id: str | None = None
            while ancestor_id is not None and ancestor_id in subtree_ids:
                ancestor = nodes_by_id[ancestor_id]
                if ancestor.kind == "session":
                    nearest_internal_session_id = ancestor.node_id
                    break
                ancestor_id = ancestor.parent_node_id
            expected[node.node_id] = (
                nearest_internal_session_id or external_parent_session_id
            )
        return expected

    def delete_folder(self, folder_id: str) -> None:
        """删除空 folder（非递归）；非空/非 folder/非 active 均 RuntimeError。

        旧签名的 ``deleting_subtree_id`` 形参整体移除：新模型递归删除统一
        走子树删除协议（:meth:`begin_subtree_delete` /
        :meth:`finish_subtree_delete`），切换轮适配。
        """
        self._store.delete_empty_folder(folder_id)
    # 删除适配（R14 子树删除协议）
    # ------------------------------------------------------------------

    def begin_subtree_delete(self, folder_id: str) -> None:
        """冻结 folder 子树：create record + mark（整树 deleting）。

        mark 的单事务 CAS（active→deleting + revision+1）是唯一逻辑可见
        性关闭点；idempotency_key=uuid4 记入实例表，供
        :meth:`finish_subtree_delete` 定位。
        """
        with self._lock:
            if folder_id in self._subtree_delete_keys:
                raise RuntimeError(f"会话文件夹子树已有删除操作: {folder_id}")
            node = self._store.get_node(folder_id)
            if node.kind != "folder":
                raise RuntimeError(f"节点不是会话文件夹: {folder_id}")
            idempotency_key = uuid.uuid4().hex
            self._store.create_or_get_subtree_delete_record(
                idempotency_key=idempotency_key,
                workspace_id=self._workspace_id,
                root_node_id=folder_id,
            )
            self._store.mark_subtree_deleting(idempotency_key)
            self._subtree_delete_keys[folder_id] = idempotency_key

    async def finish_subtree_delete(self, folder_id: str) -> None:
        """对应 :meth:`begin_subtree_delete`：drain（fence CAS + 物理隔离）
        + finish（tombstone）。

        无对应 begin 记录 → ``RuntimeError``；drain/finish 失败保留实例
        表条目，调用方可按 record 定点重入（幂等恢复）。
        """
        with self._lock:
            idempotency_key = self._subtree_delete_keys.get(folder_id)
            if idempotency_key is None:
                raise RuntimeError(f"会话文件夹子树删除锁不存在: {folder_id}")
        await self._delete_service.delete(
            idempotency_key=idempotency_key,
            root_node_id=folder_id,
        )
        with self._lock:
            self._subtree_delete_keys.pop(folder_id, None)

    async def delete_session_subtree(self, session_id: str) -> list[str]:
        """删除完整会话子树（R14 协议单调用版），返回被删后代 session ID。"""
        with self._lock:
            node = self._store.get_node(session_id)
            if node.kind != "session":
                raise RuntimeError(f"节点不是会话: {session_id}")
            idempotency_key = uuid.uuid4().hex
        result = await self._delete_service.delete(
            idempotency_key=idempotency_key,
            root_node_id=session_id,
        )
        return sorted(
            candidate_id
            for candidate_id in result.drained_session_ids
            if candidate_id != session_id
        )
