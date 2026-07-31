from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.abstractions.job_service import JobServiceProtocol
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.session_paths import (
    FOLDER_MANIFEST_NAME,
    SessionPathResolver,
    SessionPhysicalNode,
)
from app.schemas.public_v2.session_navigation import (
    SessionCatalogBreadcrumbDTO,
    SessionCatalogExportDTO,
    SessionCatalogNodeDTO,
    SessionCatalogPageDTO,
    SessionCatalogSearchResultDTO,
    SessionCatalogSearchResultsDTO,
    SessionFolderCreateRequest,
    SessionFolderUpdateRequest,
)
from app.services.business.session_service import SessionService
from app.services.business.session_resource_service import SessionResourceService


T = TypeVar("T")


class SessionCatalogService:
    """把权威会话索引投影为可分页、可搜索的目录 API。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        job_service: JobServiceProtocol | None = None,
        background_task_registry: BackgroundTaskRegistry | None = None,
    ) -> None:
        self._session_service = session_service
        self._path_resolver: SessionPathResolver = session_service.path_resolver
        self._job_service = job_service
        self._background_task_registry = background_task_registry
        self._session_resource_service: SessionResourceService | None = None
        self._cached_nodes: list[SessionCatalogNodeDTO] | None = None
        self._cached_revision: str | None = None
        self._cached_physical_revision: int | None = None
        self._session_service.register_change_listener(self._on_session_changed)

    def invalidate(self) -> None:
        self._cached_nodes = None
        self._cached_revision = None
        self._cached_physical_revision = None

    @property
    def path_resolver(self) -> SessionPathResolver:
        return self._path_resolver

    def _on_session_changed(self, action: str, session_id: str) -> None:
        self.invalidate()

    async def refresh(self) -> SessionCatalogPageDTO:
        self._path_resolver.refresh()
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
        if payload.parent_folder_id is not None:
            self._path_resolver.get_node(payload.parent_folder_id)
        folder = self._path_resolver.create_folder(
            name=payload.name,
            parent_node_id=payload.parent_folder_id,
        )
        self.invalidate()
        return await self.breadcrumb(folder.node_id)

    async def update_folder(
        self,
        folder_id: str,
        payload: SessionFolderUpdateRequest,
    ) -> SessionCatalogBreadcrumbDTO:
        folder = self._path_resolver.get_node(folder_id)
        if folder.kind != "folder":
            raise KeyError(f"会话文件夹不存在: {folder_id}")
        parent_node_id = (
            payload.parent_folder_id
            if "parent_folder_id" in payload.model_fields_set
            else folder.parent_node_id
        )
        name = payload.name if payload.name is not None else folder.name
        async def move_folder() -> SessionPhysicalNode:
            if parent_node_id != folder.parent_node_id:
                return await self._session_service.relocate_folder_tree(
                    folder_id=folder_id,
                    parent_node_id=parent_node_id,
                    name=name,
                )
            return self._path_resolver.move_node(
                node_id=folder_id,
                parent_node_id=parent_node_id,
                name=name,
            )

        moved = await self._run_sessions_idle(
            self._descendant_session_ids(folder_id),
            move_folder,
        )
        self.invalidate()
        return await self.breadcrumb(moved.node_id)

    async def assign_session(
        self,
        session_id: str,
        folder_id: str | None,
    ) -> SessionCatalogBreadcrumbDTO:
        await self._session_service.move_to_folder(session_id, folder_id)
        self.invalidate()
        return await self.breadcrumb(session_id)

    async def move_node(
        self,
        node_id: str,
        parent_node_id: str | None,
    ) -> SessionCatalogBreadcrumbDTO:
        """按目标节点类型移动会话或会话文件夹。"""
        node = self._path_resolver.get_node(node_id)
        parent = (
            self._path_resolver.get_node(parent_node_id)
            if parent_node_id is not None
            else None
        )
        if node.kind == "folder":
            return await self.update_folder(
                node_id,
                SessionFolderUpdateRequest(parent_folder_id=parent_node_id),
            )
        await self._session_service.move_session(
            node_id,
            parent.node_id if parent is not None else None,
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
        for raw_segment in path_segments:
            segment = raw_segment.strip()
            if not segment:
                raise ValueError("会话目录路径段不能为空")
            nodes = self._path_resolver.list_nodes()
            matches = [
                node
                for node in nodes
                if node.kind == "folder"
                and node.parent_node_id == parent_node_id
                and node.name == segment
            ]
            if len(matches) > 1:
                raise RuntimeError(
                    f"物理目录存在同名兄弟文件夹: parent={parent_node_id}, name={segment}"
                )
            if matches:
                parent_node_id = matches[0].node_id
                continue
            created = self._path_resolver.create_folder(
                name=segment,
                parent_node_id=parent_node_id,
            )
            parent_node_id = created.node_id
        self.invalidate()
        return parent_node_id

    def bind_session_resource_service(
        self,
        session_resource_service: SessionResourceService,
    ) -> None:
        self._session_resource_service = session_resource_service

    async def delete_folder(
        self,
        folder_id: str,
        *,
        recursive: bool = False,
    ) -> None:
        folder = self._path_resolver.get_node(folder_id)
        if folder.kind != "folder":
            raise KeyError(f"会话文件夹不存在: {folder_id}")
        if recursive:
            await self._delete_folder_tree(folder_id)
            self.invalidate()
            return
        try:
            self._path_resolver.delete_folder(folder_id)
        except RuntimeError as error:
            raise ValueError(str(error)) from error
        self.invalidate()

    async def _delete_folder_tree(self, folder_id: str) -> None:
        self._path_resolver.begin_subtree_delete(folder_id)
        try:
            await self._delete_frozen_folder_tree(folder_id)
        finally:
            self._path_resolver.finish_subtree_delete(folder_id)

    async def _delete_frozen_folder_tree(self, folder_id: str) -> None:
        nodes = self._path_resolver.list_nodes()
        nodes_by_id = {node.node_id: node for node in nodes}
        descendant_ids: set[str] = {folder_id}
        pending = [folder_id]
        while pending:
            parent_id = pending.pop()
            for node in nodes:
                if node.parent_node_id != parent_id:
                    continue
                if node.node_id in descendant_ids:
                    raise RuntimeError(
                        f"会话物理目录包含循环或重复子节点: {node.node_id}"
                    )
                descendant_ids.add(node.node_id)
                pending.append(node.node_id)
        subtree = [nodes_by_id[node_id] for node_id in descendant_ids]
        session_nodes = [node for node in subtree if node.kind == "session"]
        folder_nodes = [node for node in subtree if node.kind == "folder"]
        self._validate_managed_folder_tree(folder_nodes, subtree)
        session_ids = sorted(node.node_id for node in session_nodes)
        if session_ids and (
            self._job_service is None or self._session_resource_service is None
        ):
            raise RuntimeError("递归删除会话文件夹缺少 Job 或资源清理服务")

        async def delete_prepared() -> None:
            if self._background_task_registry is not None:
                blockers = [
                    handle
                    for handle in self._background_task_registry.list_active_handles()
                    if handle.session_id in set(session_ids)
                ]
                if blockers:
                    raise RuntimeError(
                        "会话存在运行中后台任务，拒绝递归删除: "
                        + ",".join(
                            f"{handle.session_id}:{handle.task_id}"
                            for handle in blockers
                        )
                    )
            if self._session_resource_service is not None:
                for session_id in session_ids:
                    await self._session_resource_service.cleanup_session(session_id)
            for node in sorted(
                subtree,
                key=lambda node: len(node.path.parts),
                reverse=True,
            ):
                if node.kind == "session":
                    await self._session_service.delete(node.node_id)
                else:
                    self._path_resolver.delete_folder(
                        node.node_id,
                        deleting_subtree_id=folder_id,
                    )

        if self._job_service is None:
            await delete_prepared()
        else:
            await self._job_service.run_sessions_delete_operation(
                session_ids,
                delete_prepared,
            )

    @staticmethod
    def _validate_managed_folder_tree(
        folder_nodes: list[SessionPhysicalNode],
        subtree: list[SessionPhysicalNode],
    ) -> None:
        children_by_parent: dict[str, list[SessionPhysicalNode]] = {}
        for node in subtree:
            if node.parent_node_id is not None:
                children_by_parent.setdefault(node.parent_node_id, []).append(node)
        for folder in folder_nodes:
            allowed = {
                folder.path / FOLDER_MANIFEST_NAME,
                *(child.path for child in children_by_parent.get(folder.node_id, [])),
            }
            unmanaged = [entry for entry in folder.path.iterdir() if entry not in allowed]
            if unmanaged:
                raise RuntimeError(
                    "会话文件夹包含未托管内容，拒绝递归删除: "
                    f"folder_id={folder.node_id}, entries="
                    f"{','.join(str(entry) for entry in unmanaged)}"
                )

    async def _run_sessions_idle(
        self,
        session_ids: list[str],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        if not session_ids:
            return await operation()

        async def reject_active_background_tasks() -> T:
            if self._background_task_registry is not None:
                target_ids = set(session_ids)
                blockers = [
                    handle
                    for handle in self._background_task_registry.list_active_handles()
                    if handle.session_id in target_ids
                ]
                if blockers:
                    details = ",".join(
                        f"{handle.session_id}:{handle.task_id}"
                        for handle in blockers
                    )
                    raise RuntimeError(
                        "会话存在运行中后台任务，不能移动物理存储: "
                        f"{details}"
                    )
            return await operation()

        if self._job_service is None:
            return await reject_active_background_tasks()
        return await self._job_service.run_sessions_idle_operation(
            session_ids,
            reject_active_background_tasks,
        )

    def _descendant_session_ids(self, node_id: str) -> list[str]:
        nodes = self._path_resolver.list_nodes()
        children_by_parent: dict[str, list[SessionPhysicalNode]] = {}
        for node in nodes:
            if node.parent_node_id is None:
                continue
            children_by_parent.setdefault(node.parent_node_id, []).append(node)
        session_ids: list[str] = []
        pending = [node_id]
        visited: set[str] = set()
        while pending:
            current_id = pending.pop()
            if current_id in visited:
                raise RuntimeError(f"会话物理目录包含循环关系: {current_id}")
            visited.add(current_id)
            for child in children_by_parent.get(current_id, []):
                if child.kind == "session":
                    session_ids.append(child.node_id)
                pending.append(child.node_id)
        return sorted(session_ids)

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
        physical_nodes = self._path_resolver.list_nodes(refresh=force)
        physical_revision = self._path_resolver.revision
        child_parent_ids = {
            node.parent_node_id
            for node in physical_nodes
            if node.parent_node_id is not None
        }
        nodes = [self._to_catalog_node(node, child_parent_ids) for node in physical_nodes]
        nodes_by_id = {node.node_id: node for node in nodes}
        self._validate_parent_graph(nodes, nodes_by_id)
        revision = self._revision(nodes)
        self._cached_nodes = nodes
        self._cached_revision = revision
        self._cached_physical_revision = physical_revision
        return nodes, revision

    def _to_catalog_node(
        self,
        node: SessionPhysicalNode,
        child_parent_ids: set[str],
    ) -> SessionCatalogNodeDTO:
        return SessionCatalogNodeDTO(
            node_id=node.node_id,
            kind=node.kind,
            name=node.name,
            parent_node_id=node.parent_node_id,
            session_id=node.node_id if node.kind == "session" else None,
            folder_id=node.node_id if node.kind == "folder" else None,
            has_children=node.node_id in child_parent_ids,
            storage_relative_path=node.path.relative_to(
                self._path_resolver.sessions_root
            ).as_posix(),
            created_at=node.created_at,
            updated_at=node.updated_at,
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
            raise ValueError("会话目录 cursor 格式无效")
        if payload.get("revision") != revision:
            raise ValueError("会话目录已更新，请从第一页重新加载")
        offset = payload.get("offset")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("会话目录 cursor offset 无效")
        return offset
