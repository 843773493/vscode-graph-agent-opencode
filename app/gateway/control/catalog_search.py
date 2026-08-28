from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_gateway_root
from app.gateway.auth import LOCAL_TOKEN
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.schemas.gateway_control import (
    GatewaySessionSearchMatchDTO,
    GatewaySessionSearchResultsDTO,
    GatewaySessionSearchWorkspaceStatusDTO,
)
from app.gateway.control.storage import atomic_write_json, read_json_object
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.internal_v2.session_navigation import SessionCatalogNodeDTO

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CatalogSnapshot:
    revision: str
    updated_at: datetime
    nodes_by_id: dict[str, SessionCatalogNodeDTO]


class GatewaySessionCatalogSearchService:
    def __init__(
        self,
        *,
        registry: GatewayWorkspaceRegistry,
        http_client: httpx.AsyncClient,
        cache_dir: Path,
        navigation_store: WorkspaceNavigationStore,
        refresh_interval_seconds: float = 30,
        max_concurrency: int = 8,
        request_timeout_seconds: float = 30,
    ) -> None:
        self._registry = registry
        self._http_client = http_client
        self._cache_dir = cache_dir
        self._navigation_store = navigation_store
        self._refresh_interval_seconds = refresh_interval_seconds
        self._max_concurrency = max_concurrency
        self._request_timeout_seconds = request_timeout_seconds
        self._snapshots: dict[str, _CatalogSnapshot] = {}
        self._fresh_workspace_ids: set[str] = set()
        self._workspace_errors: dict[str, str] = {}
        self._sync_locks: dict[str, asyncio.Lock] = {}
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Gateway 会话目录索引同步器已经启动")
        for target in self._registry.targets():
            snapshot = self._load_snapshot(target.workspace_id)
            if snapshot is not None:
                self._snapshots[target.workspace_id] = snapshot
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="gateway-session-catalog-index-sync",
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def search(
        self,
        query: str,
        *,
        limit_per_workspace: int,
        request_id: str,
    ) -> GatewaySessionSearchResultsDTO:
        normalized = query.strip().casefold()
        if not normalized:
            raise ValueError("跨工作区会话搜索词不能为空")
        results = await asyncio.gather(
            *(
                self._search_workspace(
                    target,
                    normalized,
                    limit=limit_per_workspace,
                    request_id=request_id,
                )
                for target in self._registry.targets()
            )
        )
        items = [item for matches, _status in results for item in matches]
        items.extend(
            self._search_workspace_folders(
                normalized,
                limit=limit_per_workspace,
            )
        )
        items.sort(
            key=lambda item: (
                item.workspace_name.casefold(),
                item.relative_path.casefold(),
            )
        )
        return GatewaySessionSearchResultsDTO(
            items=items,
            workspaces=[status for _matches, status in results],
            total=len(items),
        )

    def _search_workspace_folders(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[GatewaySessionSearchMatchDTO]:
        tree = self._navigation_store.list_tree(self._registry.targets())
        nodes_by_id = {node.node_id: node for node in tree.nodes}
        matches: list[GatewaySessionSearchMatchDTO] = []
        for node in tree.nodes:
            if node.kind != "workspace_folder" or query not in (
                f"{node.name} {node.node_id}"
            ).casefold():
                continue
            breadcrumb = []
            current = node
            visited: set[str] = set()
            while current is not None:
                if current.node_id in visited:
                    raise RuntimeError(
                        f"Gateway 工作区目录包含循环: {current.node_id}"
                    )
                visited.add(current.node_id)
                breadcrumb.append(current)
                current = (
                    nodes_by_id.get(current.parent_node_id)
                    if current.parent_node_id is not None
                    else None
                )
            breadcrumb.reverse()
            matches.append(
                GatewaySessionSearchMatchDTO(
                    workspace_id="gateway-navigation",
                    workspace_name="工作区目录",
                    node_id=node.node_id,
                    node_kind="workspace_folder",
                    name=node.name,
                    relative_path="/".join(item.name for item in breadcrumb),
                    breadcrumb_names=[item.name for item in breadcrumb],
                    breadcrumb_node_ids=[item.node_id for item in breadcrumb],
                )
            )
        matches.sort(key=lambda item: (item.relative_path.casefold(), item.node_id))
        return matches[:limit]

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._sync_all()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._refresh_interval_seconds,
                )
            except TimeoutError:
                continue

    async def _sync_all(self) -> None:
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def sync(target: WorkspaceTarget) -> None:
            async with semaphore:
                await self._sync_workspace(
                    target,
                    request_id=create_prefixed_id("req"),
                )

        targets = self._registry.targets()
        results = await asyncio.gather(
            *(sync(target) for target in targets),
            return_exceptions=True,
        )
        for target, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                message = f"{type(result).__name__}: {result}"
                self._workspace_errors[target.workspace_id] = message
                self._fresh_workspace_ids.discard(target.workspace_id)
                logger.warning(
                    "同步工作区会话目录索引失败: workspace_id=%s, error=%s",
                    target.workspace_id,
                    message,
                )

    async def _search_workspace(
        self,
        target: WorkspaceTarget,
        query: str,
        *,
        limit: int,
        request_id: str,
    ) -> tuple[
        list[GatewaySessionSearchMatchDTO],
        GatewaySessionSearchWorkspaceStatusDTO,
    ]:
        snapshot = self._snapshots.get(target.workspace_id)
        if snapshot is None:
            try:
                snapshot = await self._sync_workspace(
                    target,
                    request_id=request_id,
                )
            except (httpx.HTTPError, LookupError, ValueError, RuntimeError) as error:
                message = f"{type(error).__name__}: {error}"
                self._workspace_errors[target.workspace_id] = message
                return [], GatewaySessionSearchWorkspaceStatusDTO(
                    workspace_id=target.workspace_id,
                    workspace_name=target.name,
                    status="unavailable",
                    error=message,
                )
        matches = self._search_snapshot(target, snapshot, query, limit=limit)
        if query in target.name.casefold():
            matches.insert(
                0,
                GatewaySessionSearchMatchDTO(
                    workspace_id=target.workspace_id,
                    workspace_name=target.name,
                    node_id=f"workspace:{target.workspace_id}",
                    node_kind="workspace",
                    name=target.name,
                    relative_path=target.name,
                ),
            )
        if target.workspace_id in self._fresh_workspace_ids:
            status = "available"
            error_message = None
        else:
            status = "stale"
            error_message = self._workspace_errors.get(
                target.workspace_id,
                f"使用 {snapshot.updated_at.isoformat()} 的本地持久化索引",
            )
        return matches, GatewaySessionSearchWorkspaceStatusDTO(
            workspace_id=target.workspace_id,
            workspace_name=target.name,
            status=status,
            error=error_message,
        )

    async def _sync_workspace(
        self,
        target: WorkspaceTarget,
        *,
        request_id: str,
    ) -> _CatalogSnapshot:
        lock = self._sync_locks.setdefault(target.workspace_id, asyncio.Lock())
        async with lock:
            url, headers = self._target_request(target, request_id=request_id)
            response = await self._http_client.get(
                url,
                headers=headers,
                timeout=self._request_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            raw_items = data.get("items") if isinstance(data, dict) else None
            revision = data.get("revision") if isinstance(data, dict) else None
            if not isinstance(raw_items, list) or not isinstance(revision, str):
                raise RuntimeError(
                    "工作区目录导出响应缺少 data.revision/items: "
                    f"workspace_id={target.workspace_id}"
                )
            nodes = [SessionCatalogNodeDTO.model_validate(item) for item in raw_items]
            nodes_by_id = {node.node_id: node for node in nodes}
            if len(nodes_by_id) != len(nodes):
                raise RuntimeError(
                    f"工作区目录导出包含重复 node_id: {target.workspace_id}"
                )
            snapshot = _CatalogSnapshot(
                revision=revision,
                updated_at=datetime.now(timezone.utc),
                nodes_by_id=nodes_by_id,
            )
            self._save_snapshot(target.workspace_id, snapshot)
            self._snapshots[target.workspace_id] = snapshot
            self._fresh_workspace_ids.add(target.workspace_id)
            self._workspace_errors.pop(target.workspace_id, None)
            return snapshot

    @staticmethod
    def _search_snapshot(
        target: WorkspaceTarget,
        snapshot: _CatalogSnapshot,
        query: str,
        *,
        limit: int,
    ) -> list[GatewaySessionSearchMatchDTO]:
        matching_nodes = [
            node
            for node in snapshot.nodes_by_id.values()
            if query
            in (
                f"{node.name} {node.node_id} {node.session_id or ''} "
                f"{node.storage_relative_path or ''}"
            ).casefold()
        ]
        matching_nodes.sort(key=lambda node: (node.name.casefold(), node.node_id))
        return [
            GatewaySessionCatalogSearchService._map_match(
                target,
                node,
                snapshot.nodes_by_id,
            )
            for node in matching_nodes[:limit]
        ]

    @staticmethod
    def _map_match(
        target: WorkspaceTarget,
        node: SessionCatalogNodeDTO,
        nodes_by_id: dict[str, SessionCatalogNodeDTO],
    ) -> GatewaySessionSearchMatchDTO:
        breadcrumb: list[SessionCatalogNodeDTO] = []
        current: SessionCatalogNodeDTO | None = node
        visited: set[str] = set()
        while current is not None:
            if current.node_id in visited:
                raise RuntimeError(
                    f"工作区会话目录包含循环关系: {target.workspace_id}/{current.node_id}"
                )
            visited.add(current.node_id)
            breadcrumb.append(current)
            if current.parent_node_id is None:
                current = None
                continue
            parent = nodes_by_id.get(current.parent_node_id)
            if parent is None:
                raise RuntimeError(
                    "工作区会话目录父节点不存在: "
                    f"workspace_id={target.workspace_id}, "
                    f"node_id={current.node_id}, parent_id={current.parent_node_id}"
                )
            current = parent
        breadcrumb.reverse()
        names = [item.name for item in breadcrumb]
        node_ids = [item.node_id for item in breadcrumb]
        return GatewaySessionSearchMatchDTO(
            workspace_id=target.workspace_id,
            workspace_name=target.name,
            node_id=node.node_id,
            node_kind=node.kind,
            name=node.name,
            session_id=node.session_id,
            relative_path="/".join(names),
            storage_relative_path=node.storage_relative_path,
            breadcrumb_names=names,
            breadcrumb_node_ids=node_ids,
        )

    def _load_snapshot(self, workspace_id: str) -> _CatalogSnapshot | None:
        path = self._cache_path(workspace_id)
        if not path.exists():
            return None
        payload = read_json_object(path, default={})
        if payload.get("workspace_id") != workspace_id:
            raise RuntimeError(f"Gateway 会话目录索引 workspace_id 不匹配: {path}")
        revision = payload.get("revision")
        updated_at = payload.get("updated_at")
        raw_items = payload.get("items")
        if (
            not isinstance(revision, str)
            or not isinstance(updated_at, str)
            or not isinstance(raw_items, list)
        ):
            raise RuntimeError(f"Gateway 会话目录索引格式无效: {path}")
        nodes = [SessionCatalogNodeDTO.model_validate(item) for item in raw_items]
        nodes_by_id = {node.node_id: node for node in nodes}
        if len(nodes_by_id) != len(nodes):
            raise RuntimeError(f"Gateway 会话目录索引包含重复 node_id: {path}")
        return _CatalogSnapshot(
            revision=revision,
            updated_at=datetime.fromisoformat(updated_at),
            nodes_by_id=nodes_by_id,
        )

    def _save_snapshot(self, workspace_id: str, snapshot: _CatalogSnapshot) -> None:
        atomic_write_json(
            self._cache_path(workspace_id),
            {
                "schema_version": 1,
                "workspace_id": workspace_id,
                "revision": snapshot.revision,
                "updated_at": snapshot.updated_at.isoformat(),
                "items": [
                    node.model_dump(mode="json")
                    for node in snapshot.nodes_by_id.values()
                ],
            },
        )

    def _cache_path(self, workspace_id: str) -> Path:
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _target_request(
        self,
        target: WorkspaceTarget,
        *,
        request_id: str,
    ) -> tuple[str, dict[str, str]]:
        if target.connection_kind == "remote_gateway":
            if (
                target.remote_gateway_connection_id is None
                or target.remote_workspace_id is None
            ):
                raise RuntimeError(
                    f"远程工作区投影缺少路由信息: {target.workspace_id}"
                )
            credential = FederationCredentialStore(
                storage_path=get_gateway_root() / "credentials" / "federation.json"
            ).get(target.remote_gateway_connection_id)
            return (
                f"{self._registry.remote_gateway_url(target.remote_gateway_connection_id)}"
                "/api/v1/session-catalog/export",
                {
                    "X-BoxTeam-Workspace-Id": target.remote_workspace_id,
                    "X-BoxTeam-Federation-Token": credential.token,
                    "X-Request-ID": request_id,
                },
            )
        return (
            f"{target.backend_url.rstrip('/')}/api/v1/session-catalog/export",
            {"X-Local-Token": LOCAL_TOKEN, "X-Request-ID": request_id},
        )
