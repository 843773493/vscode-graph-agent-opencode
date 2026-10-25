"""会话目录读侧快照、投影、搜索与分页。"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from heapq import nsmallest

from app.core.session_catalog_resolver import (
    SessionCatalogNodeProjection,
    SessionCatalogPathResolver,
    SessionCatalogSessionProjection,
)
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogBreadcrumbDTO,
    SessionCatalogExportDTO,
    SessionCatalogNodeDTO,
    SessionCatalogPageDTO,
    SessionCatalogSearchResultDTO,
    SessionCatalogSearchResultsDTO,
)
from app.services.business.session_service import SessionService

_SEARCH_TOP_K_MIN_NODE_COUNT = 2_000
_SEARCH_TOP_K_MIN_SCAN_RATIO = 16


@dataclass(frozen=True)
class _SessionCatalogReadSnapshot:
    catalog_revision: int
    revision: str
    nodes: list[SessionCatalogNodeDTO]
    # 与 nodes 同序：名称、节点 ID、物理存储相对路径均已 casefold。
    folded_search_terms: tuple[tuple[str, str, str], ...]
    nodes_by_id: dict[str, SessionCatalogNodeDTO]
    visible_node_ids: frozenset[str]
    child_node_ids_by_parent: dict[str | None, tuple[str, ...]]
    default_provider_nodes: list[SessionCatalogSessionProjection]


class SessionCatalogReadService:
    """持有会话目录读快照并提供分页、搜索和投影。"""

    def __init__(self, *, session_service: SessionService) -> None:
        self._session_service = session_service
        self._path_resolver: SessionCatalogPathResolver = session_service.path_resolver
        self._cached_snapshot: _SessionCatalogReadSnapshot | None = None

    def invalidate(self) -> None:
        self._cached_snapshot = None

    async def refresh(self) -> SessionCatalogPageDTO:
        self.invalidate()
        snapshot = await self._snapshot(force=True)
        root_node_ids = snapshot.child_node_ids_by_parent.get(None, ())
        # 刷新语义是「重载根页并校验索引」：没有 limit/cursor 入参，必须返回
        # 完整根页且 cursor 为空，否则超过上限的根节点会既不在 items 里、也没
        # 有可继续分页的 cursor，被静默吞掉。分页由 list_children 承担。
        return self._build_page(
            root_node_ids,
            snapshot.nodes_by_id,
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
        if (
            parent_node_id is not None
            and parent_node_id not in snapshot.visible_node_ids
        ):
            raise KeyError(f"会话目录节点不存在: {parent_node_id}")
        child_node_ids = snapshot.child_node_ids_by_parent.get(parent_node_id, ())
        # cursor 绑定发牌时的分页作用域（父节点）：同一个 revision 下把某个父节点的
        # cursor 拿去翻另一个父节点的页，会让 offset 落到错误切片的中间，静默跳项。
        scope = parent_node_id
        offset = self._decode_cursor(
            cursor,
            snapshot.revision,
            snapshot.catalog_revision,
            scope,
            collection_size=len(child_node_ids),
        )
        return self._build_page(
            child_node_ids,
            snapshot.nodes_by_id,
            parent_node_id=parent_node_id,
            offset=offset,
            limit=limit,
            revision=snapshot.revision,
            catalog_revision=snapshot.catalog_revision,
            cursor_scope=scope,
        )

    def _build_page(
        self,
        child_node_ids: tuple[str, ...],
        nodes_by_id: dict[str, SessionCatalogNodeDTO],
        *,
        parent_node_id: str | None,
        offset: int,
        limit: int | None,
        revision: str,
        catalog_revision: int,
        cursor_scope: str | None,
    ) -> SessionCatalogPageDTO:
        """构造目录分页页；limit 为 None 表示返回 offset 之后的全部节点。"""
        page_node_ids = (
            child_node_ids[offset:]
            if limit is None
            else child_node_ids[offset : offset + limit]
        )
        page = [nodes_by_id[node_id] for node_id in page_node_ids]
        total = len(child_node_ids)
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
                if next_offset < total
                else None
            ),
            total=total,
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
        match_count = 0

        def matching_nodes() -> Iterator[
            tuple[tuple[str, str, str], SessionCatalogNodeDTO]
        ]:
            nonlocal match_count
            for (folded_name, folded_node_id, folded_storage_path), node in zip(
                snapshot.folded_search_terms, snapshot.nodes, strict=True
            ):
                if (
                    normalized_query not in folded_name
                    and normalized_query not in folded_node_id
                    and normalized_query not in folded_storage_path
                ):
                    continue
                match_count += 1
                yield (folded_name, folded_storage_path, node.node_id), node

        if (
            cursor is None
            and limit > 0
            and len(snapshot.nodes)
            > max(_SEARCH_TOP_K_MIN_NODE_COUNT, limit * _SEARCH_TOP_K_MIN_SCAN_RATIO)
        ):
            # 大目录首屏只保留排序最靠前的一页，仍扫描全目录以准确报告 total。
            page_matches = nsmallest(
                limit,
                matching_nodes(),
                key=lambda match: match[0],
            )
            offset = 0
        else:
            matches = list(matching_nodes())
            matches.sort(key=lambda match: match[0])
            offset = (
                0
                if cursor is None
                else self._decode_cursor(
                    cursor,
                    snapshot.revision,
                    snapshot.catalog_revision,
                    scope,
                    collection_size=match_count,
                )
            )
            page_matches = matches[offset : offset + limit]

        page_nodes = [node for _, node in page_matches]
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
                if next_offset < match_count
                else None
            ),
            total=match_count,
        )

    async def export_index(self) -> SessionCatalogExportDTO:
        snapshot = await self._snapshot()
        return SessionCatalogExportDTO(
            revision=snapshot.revision,
            catalog_revision=snapshot.catalog_revision,
            items=snapshot.nodes,
        )

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
                    if node.node_id in replacements
                    else node
                    for node in self._cached_snapshot.nodes
                ]
                self._cached_snapshot = replace(
                    self._cached_snapshot,
                    nodes=nodes,
                    nodes_by_id={
                        **self._cached_snapshot.nodes_by_id,
                        **{node.node_id: node for node in nodes},
                    },
                    revision=self._revision(nodes),
                )
            return self._cached_snapshot

        catalog_revision, catalog_nodes = self._path_resolver.catalog_snapshot()
        visible_catalog_nodes = self._visible_catalog_nodes(catalog_nodes)
        visible_parent_ids_with_children = {
            node.parent_node_id
            for node in visible_catalog_nodes
            if node.parent_node_id is not None
        }
        nodes = [
            self._to_catalog_node(node, visible_parent_ids_with_children)
            for node in visible_catalog_nodes
        ]
        nodes_by_id = {node.node_id: node for node in nodes}
        all_nodes_by_id = dict(nodes_by_id)
        all_nodes_by_id.update(
            {
                catalog_node.node_id: self._to_pending_catalog_node(
                    catalog_node, visible_parent_ids_with_children
                )
                for catalog_node in catalog_nodes
                if catalog_node.node_id not in nodes_by_id
            }
        )
        self._validate_parent_graph(list(all_nodes_by_id.values()), all_nodes_by_id)
        revision = self._revision(nodes)
        child_node_ids_by_parent = self._index_sorted_child_ids(nodes)
        session_storage_paths = {
            node.node_id: node.storage_relative_path
            for node in visible_catalog_nodes
            if isinstance(node, SessionCatalogSessionProjection)
        }
        snapshot = _SessionCatalogReadSnapshot(
            catalog_revision=catalog_revision,
            revision=revision,
            nodes=nodes,
            folded_search_terms=tuple(
                (
                    node.name.casefold(),
                    node.node_id.casefold(),
                    session_storage_paths.get(node.node_id, "").casefold(),
                )
                for node in nodes
            ),
            nodes_by_id=all_nodes_by_id,
            visible_node_ids=frozenset(node.node_id for node in nodes),
            child_node_ids_by_parent=child_node_ids_by_parent,
            default_provider_nodes=[
                node
                for node in visible_catalog_nodes
                if isinstance(node, SessionCatalogSessionProjection)
                and node.current_provider_id is None
            ],
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
            if isinstance(node, SessionCatalogSessionProjection)
            else None
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
    ) -> None:
        validated: set[str] = set()
        for node in nodes:
            current: SessionCatalogNodeDTO | None = node
            chain: set[str] = set()
            while current is not None and current.node_id not in validated:
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

    @staticmethod
    def _index_sorted_child_ids(
        nodes: list[SessionCatalogNodeDTO],
    ) -> dict[str | None, tuple[str, ...]]:
        children_by_parent: dict[str | None, list[SessionCatalogNodeDTO]] = {}
        for node in nodes:
            children_by_parent.setdefault(node.parent_node_id, []).append(node)
        return {
            parent_node_id: tuple(
                node.node_id
                for node in sorted(
                    children,
                    key=lambda node: (
                        node.kind != "folder",
                        node.name.casefold(),
                        node.node_id,
                    ),
                )
            )
            for parent_node_id, children in children_by_parent.items()
        }

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
