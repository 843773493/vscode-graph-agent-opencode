"""会话目录读侧快照、投影、搜索与分页。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import cast

from app.core.session_catalog_resolver import (
    SessionCatalogNodeProjection,
    SessionCatalogPathResolver,
    SessionCatalogSessionProjection,
)
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogBreadcrumbDTO,
    SessionCatalogExportDTO,
    SessionCatalogNodeDTO,
    SessionCatalogPageDTO,
    SessionCatalogSearchResultsDTO,
    SessionCatalogVersionDTO,
)
from app.services.business.session_navigation.catalog_query import (
    catalog_projection_revision,
    decode_catalog_cursor,
    encode_catalog_cursor,
    search_catalog,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget
from app.services.business.session_service import SessionService
from app.services.mapping.session_catalog_search import (
    CatalogSearchTerms,
    catalog_search_terms,
)


@dataclass(frozen=True)
class _SessionCatalogReadSnapshot:
    catalog_revision: int
    revision: str
    nodes: list[SessionCatalogNodeDTO]
    folded_search_terms: dict[str, CatalogSearchTerms]
    nodes_by_id: dict[str, SessionCatalogNodeDTO]
    default_model_nodes: list[SessionCatalogSessionProjection]


class SessionCatalogReadService:
    """持有会话目录读快照并提供分页、搜索和投影。"""

    def __init__(self, *, session_service: SessionService) -> None:
        self._session_service = session_service
        self._path_resolver: SessionCatalogPathResolver = session_service.path_resolver
        self._cached_snapshot: _SessionCatalogReadSnapshot | None = None
        self._snapshot_lock = asyncio.Lock()

    @property
    def workspace_id(self) -> str:
        return self._path_resolver.workspace_id

    def invalidate(self) -> None:
        self._cached_snapshot = None

    def version(self) -> SessionCatalogVersionDTO:
        """版本确认只读两个已存在的标量，不投影、hash 或导出全目录。"""
        with self._session_service.catalog_projection() as projection_revision:
            return SessionCatalogVersionDTO(
                workspace_id=self.workspace_id,
                catalog_revision=self._path_resolver.revision,
                projection_revision=projection_revision,
            )

    async def refresh(self, *, minimum_catalog_revision: int = 0) -> SessionCatalogPageDTO:
        return await self.list_children(
            parent_node_id=None, limit=None, cursor=None,
            minimum_catalog_revision=minimum_catalog_revision,
        )

    async def list_children(
        self, *, parent_node_id: str | None, limit: int | None, cursor: str | None,
        minimum_catalog_revision: int = 0,
    ) -> SessionCatalogPageDTO:
        def read() -> SessionCatalogPageDTO:
            with self._session_service.catalog_projection() as projection_revision:
                scope = self._path_resolver.catalog_store.read_catalog_children(
                    parent_node_id, limit=limit, minimum_catalog_revision=minimum_catalog_revision,
                    page_offset=lambda revision, total: decode_catalog_cursor(
                        cursor, catalog_projection_revision(revision, projection_revision), revision,
                        parent_node_id, collection_size=total,
                    ),
                )
                models: dict[str, ModelReferenceDTO | None] = {}
                nodes = [self._to_catalog_node(node, scope.node_ids_with_children, models)
                         for node in self._path_resolver.project_catalog_range(scope)]
                next_offset = scope.offset + len(nodes)
                revision = catalog_projection_revision(scope.catalog_revision, projection_revision)
                return SessionCatalogPageDTO(
                    revision=revision, catalog_revision=scope.catalog_revision, parent_node_id=parent_node_id,
                    items=nodes, total=scope.total,
                    cursor=encode_catalog_cursor(next_offset, revision, scope.catalog_revision, parent_node_id)
                        if next_offset < scope.total else None,
                )
        return await run_sync_shielded(read)

    async def breadcrumb(self, node_id: str, *, minimum_catalog_revision: int = 0) -> SessionCatalogBreadcrumbDTO:
        def read() -> SessionCatalogBreadcrumbDTO:
            with self._session_service.catalog_projection() as projection_revision:
                scope = self._path_resolver.catalog_store.read_catalog_breadcrumb(
                    node_id, minimum_catalog_revision=minimum_catalog_revision,
                )
                models: dict[str, ModelReferenceDTO | None] = {}
                return SessionCatalogBreadcrumbDTO(
                    revision=catalog_projection_revision(scope.catalog_revision, projection_revision),
                    catalog_revision=scope.catalog_revision, node_id=node_id,
                    items=[self._to_catalog_node(node, scope.node_ids_with_children, models)
                           if node.node_id in scope.visible_node_ids else self._to_pending_catalog_node(node, scope.node_ids_with_children)
                           for node in self._path_resolver.project_catalog_range(scope)],
                )
        return await run_sync_shielded(read)

    async def search(
        self, *, query: str, limit: int, cursor: str | None, deadline_at: float | None = None,
    ) -> SessionCatalogSearchResultsDTO:
        if not query.strip():
            raise ValueError("会话目录搜索词不能为空")
        budget = NameSearchBudget.create(deadline_at)
        snapshot = await self._snapshot(check=budget.check)
        return await run_sync_shielded(
            search_catalog,
            nodes=snapshot.nodes, nodes_by_id=snapshot.nodes_by_id,
            folded_search_terms=snapshot.folded_search_terms,
            revision=snapshot.revision, catalog_revision=snapshot.catalog_revision,
            query=query, limit=limit, cursor=cursor, budget=budget,
        )

    async def export_index(
        self, *, node_ids: list[str] | None = None, minimum_catalog_revision: int = 0,
        check: Callable[[], None] = lambda: None,
    ) -> SessionCatalogExportDTO:
        if node_ids is None:
            snapshot = await self._snapshot(check=check)
            if snapshot.catalog_revision < minimum_catalog_revision:
                raise RuntimeError(f"会话目录权威版本低于读取下限: {snapshot.catalog_revision} < {minimum_catalog_revision}")
            return SessionCatalogExportDTO(
                revision=snapshot.revision, catalog_revision=snapshot.catalog_revision,
                items=snapshot.nodes, complete=True,
            )
        def read() -> SessionCatalogExportDTO:
            with self._session_service.catalog_projection() as projection_revision:
                check()
                scope = self._path_resolver.catalog_store.read_catalog_nodes(
                    node_ids, minimum_catalog_revision=minimum_catalog_revision,
                )
                models: dict[str, ModelReferenceDTO | None] = {}
                return SessionCatalogExportDTO(
                    revision=catalog_projection_revision(scope.catalog_revision, projection_revision),
                    catalog_revision=scope.catalog_revision, node_ids=sorted(set(node_ids)), complete=False,
                    items=[self._to_catalog_node(node, scope.node_ids_with_children, models)
                           for node in self._path_resolver.project_catalog_range(scope)
                           if node.node_id in scope.visible_node_ids],
                )
        return await run_sync_shielded(read)

    async def _snapshot(
        self,
        *,
        check: Callable[[], None] = lambda: None,
    ) -> _SessionCatalogReadSnapshot:
        # 同一时刻只构建一份投影；取消须等 worker 结算再释放锁。
        async with self._snapshot_lock:
            return await run_sync_shielded(self._read_snapshot, check=check)

    def _read_snapshot(
        self,
        *,
        check: Callable[[], None] = lambda: None,
    ) -> _SessionCatalogReadSnapshot:
        with self._session_service.catalog_projection() as projection_revision:
            check()
            current_revision = self._path_resolver.revision
            revision = catalog_projection_revision(current_revision, projection_revision)
            cached = self._cached_snapshot
            if cached is not None and cached.revision == revision:
                return cached
            if cached is not None and cached.catalog_revision == current_revision:
                models: dict[str, ModelReferenceDTO | None] = {}
                replacements: dict[str, SessionCatalogNodeDTO] = {}
                for node in cached.default_model_nodes:
                    check()
                    if node.current_agent_id not in models:
                        models[node.current_agent_id] = self._session_service.project_catalog_session(node).current_model
                    cached_node = cached.nodes_by_id[node.node_id]
                    session = cast(SessionDTO, cached_node.session)
                    replacements[node.node_id] = cached_node.model_copy(update={
                        "session": session.model_copy(update={"current_model": models[node.current_agent_id]}),
                    })
                snapshot = replace(
                    cached, revision=revision,
                    nodes=[replacements.get(node.node_id, node) for node in cached.nodes],
                    nodes_by_id={**cached.nodes_by_id, **replacements},
                )
                self._cached_snapshot = snapshot
                return snapshot

            catalog_revision, catalog_nodes = self._path_resolver.catalog_snapshot(check=check)
            visible_catalog_nodes = self._visible_catalog_nodes(catalog_nodes, check=check)
            visible_parent_ids_with_children = {
                node.parent_node_id
                for node in visible_catalog_nodes
                if node.parent_node_id is not None
            }
            nodes = []
            default_model_by_agent: dict[str, ModelReferenceDTO | None] = {}
            for node in visible_catalog_nodes:
                check()
                nodes.append(self._to_catalog_node(
                    node, visible_parent_ids_with_children, default_model_by_agent,
                ))
            nodes_by_id = {node.node_id: node for node in nodes}
            all_nodes_by_id = dict(nodes_by_id)
            for catalog_node in catalog_nodes:
                check()
                if catalog_node.node_id not in nodes_by_id:
                    all_nodes_by_id[catalog_node.node_id] = self._to_pending_catalog_node(catalog_node, visible_parent_ids_with_children)
            self._validate_parent_graph(list(all_nodes_by_id.values()), all_nodes_by_id, check=check)
            revision = catalog_projection_revision(catalog_revision, projection_revision)
            snapshot = _SessionCatalogReadSnapshot(
                catalog_revision=catalog_revision,
                revision=revision,
                nodes=nodes,
                folded_search_terms=catalog_search_terms(nodes_by_id, check=check),
                nodes_by_id=all_nodes_by_id,
                default_model_nodes=[node for node in visible_catalog_nodes
                                        if isinstance(node, SessionCatalogSessionProjection) and node.current_model_json is None],
            )
            check()
            self._cached_snapshot = snapshot
            return snapshot

    @staticmethod
    def _visible_catalog_nodes(
        nodes: list[SessionCatalogNodeProjection],
        *, check: Callable[[], None] = lambda: None,
    ) -> list[SessionCatalogNodeProjection]:
        """沿 active 父链枚举节点，隐藏 deleting 节点及其子树。"""
        children_by_parent: dict[str | None, list[SessionCatalogNodeProjection]] = {}
        for node in nodes:
            check()
            children_by_parent.setdefault(node.parent_node_id, []).append(node)

        visible: list[SessionCatalogNodeProjection] = []
        frontier: list[str | None] = [None]
        while frontier:
            check()
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
        default_model_by_agent: dict[str, ModelReferenceDTO | None],
    ) -> SessionCatalogNodeDTO:
        session_projection = (
            node if isinstance(node, SessionCatalogSessionProjection) else None
        )
        session = None
        if session_projection is not None:
            if (
                session_projection.current_model_json is None
                and session_projection.current_agent_id in default_model_by_agent
            ):
                session_projection = replace(
                    session_projection,
                    current_model_json=(default_model_by_agent[session_projection.current_agent_id].canonical_json()
                                        if default_model_by_agent[session_projection.current_agent_id] is not None else None),
                )
            session = self._session_service.project_catalog_session(session_projection)
            if session_projection.current_model_json is None:
                default_model_by_agent[session.current_agent_id] = session.current_model
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
                session_projection.created_at
                if session_projection is not None
                else None
            ),
            updated_at=(
                session_projection.updated_at
                if session_projection is not None
                else None
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
                session_projection.created_at
                if session_projection is not None
                else None
            ),
            updated_at=(
                session_projection.updated_at
                if session_projection is not None
                else None
            ),
            session=None,
        )

    @staticmethod
    def _validate_parent_graph(
        nodes: list[SessionCatalogNodeDTO],
        nodes_by_id: dict[str, SessionCatalogNodeDTO],
        *, check: Callable[[], None] = lambda: None,
    ) -> None:
        validated: set[str] = set()
        for node in nodes:
            check()
            current: SessionCatalogNodeDTO | None = node
            chain: set[str] = set()
            while current is not None and current.node_id not in validated:
                check()
                if current.node_id in chain:
                    raise RuntimeError(f"会话物理目录包含循环关系: {current.node_id}")
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
