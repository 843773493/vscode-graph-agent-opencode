from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path

import httpx

from app.core.failures import failure_detail
from app.core.identifier import create_prefixed_id
from app.core.shielded_cleanup import run_sync_shielded
from app.gateway.control.storage import atomic_write_json, read_json_object
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.registry.owners import workspace_owner
from app.gateway.runtime.consumer_protocol import GatewayRuntimeHealthProof
from app.schemas.gateway.search import GatewaySearchWorkspaceOwnerDTO
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogBreadcrumbDTO,
    SessionCatalogExportDTO,
    SessionCatalogNodeDTO,
    SessionCatalogPageDTO,
    SessionCatalogSearchResultsDTO,
    SessionCatalogSyncDTO,
    SessionCatalogVersionDTO,
)
from app.services.business.session_navigation.catalog_query import (
    catalog_breadcrumb,
    catalog_projection_revision,
    decode_catalog_cursor,
    encode_catalog_cursor,
    search_catalog,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget
from app.services.mapping.session_catalog_search import (
    CatalogSearchTerms,
    catalog_search_terms,
)

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class _CatalogSnapshot:
    owner: GatewaySearchWorkspaceOwnerDTO
    catalog_revision: int
    revision: str
    updated_at: datetime
    nodes_by_id: dict[str, SessionCatalogNodeDTO]

    @cached_property
    def children_by_parent(self) -> dict[str | None, tuple[str, ...]]:
        children: dict[str | None, list[SessionCatalogNodeDTO]] = {}
        for node in self.nodes_by_id.values():
            children.setdefault(node.parent_node_id, []).append(node)
        return {parent: tuple(node.node_id for node in sorted(nodes, key=lambda node: (node.kind != "folder", node.name.casefold(), node.node_id)))
                for parent, nodes in children.items()}

    @cached_property
    def folded_search_terms(self) -> dict[str, CatalogSearchTerms]:
        return catalog_search_terms(self.nodes_by_id)


class GatewaySessionCatalogSearchService:
    def __init__(
        self,
        *,
        registry: GatewayWorkspaceRegistry,
        http_client: httpx.AsyncClient,
        cache_dir: Path,
        gateway_id: str,
        refresh_interval_seconds: float = 30,
        max_concurrency: int = 8,
        request_timeout_seconds: float = 30,
    ) -> None:
        self._registry = registry
        self._http_client = http_client
        self._cache_dir = cache_dir
        self._gateway_id = gateway_id
        self._refresh_interval_seconds = refresh_interval_seconds
        self._max_concurrency = max_concurrency
        self._request_timeout_seconds = request_timeout_seconds
        self._snapshots: dict[GatewaySearchWorkspaceOwnerDTO, _CatalogSnapshot] = {}
        self._workspace_errors: dict[tuple[str, str], str] = {}
        self._confirmed_at: dict[GatewaySearchWorkspaceOwnerDTO, datetime] = {}
        self._in_flight: dict[GatewaySearchWorkspaceOwnerDTO, asyncio.Task[_CatalogSnapshot]] = {}
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def update_runtime_config(
        self,
        *,
        refresh_interval_seconds: float,
        max_concurrency: int,
        request_timeout_seconds: float,
    ) -> None:
        """更新下一轮目录同步使用的 Gateway 配置。"""

        self.prepare_runtime_config(
            refresh_interval_seconds=refresh_interval_seconds,
            max_concurrency=max_concurrency,
            request_timeout_seconds=request_timeout_seconds,
        )
        self._refresh_interval_seconds = refresh_interval_seconds
        self._max_concurrency = max_concurrency
        self._request_timeout_seconds = request_timeout_seconds
        self._wake_event.set()

    def prepare_runtime_config(
        self,
        *,
        refresh_interval_seconds: float,
        max_concurrency: int,
        request_timeout_seconds: float,
    ) -> None:
        """只校验目录消费者参数，不改变当前运行时。"""

        if refresh_interval_seconds <= 0:
            raise ValueError("会话目录 refresh_interval_seconds 必须大于 0")
        if max_concurrency < 1:
            raise ValueError("会话目录 max_concurrency 必须至少为 1")
        if request_timeout_seconds <= 0:
            raise ValueError("会话目录 request_timeout_seconds 必须大于 0")

    def runtime_health_proof(
        self,
        *,
        generation: str,
        fencing_token_digest: str | None = None,
    ) -> GatewayRuntimeHealthProof:
        self.assert_healthy()
        return GatewayRuntimeHealthProof(
            consumer_id="session-catalog",
            generation=generation,
            state="healthy",
            details={
                "refresh_interval_seconds": self._refresh_interval_seconds,
                "max_concurrency": self._max_concurrency,
                "request_timeout_seconds": self._request_timeout_seconds,
            },
            fencing_token_digest=fencing_token_digest,
        )

    def assert_healthy(self) -> None:
        """确认目录消费者仍有可运行的同步循环。"""

        if self._task is None or self._task.done():
            raise RuntimeError("Gateway 会话目录索引同步器未运行")

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Gateway 会话目录索引同步器已经启动")
        for target in self._registry.targets():
            owner = workspace_owner(target)
            snapshot = await run_sync_shielded(self._load_snapshot, owner)
            if snapshot is not None:
                self._snapshots[owner] = snapshot
        self._stop_event.clear()
        self._wake_event.clear()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="gateway-session-catalog-index-sync",
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        self._wake_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        pending = tuple(self._in_flight.values())
        for request in pending:
            request.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._in_flight.clear()

    def browse_mirror(
        self, target: WorkspaceTarget, *, path: list[str], query: httpx.QueryParams, request_id: str,
        known_revision: str | None = None,
    ) -> SessionCatalogPageDTO | SessionCatalogBreadcrumbDTO | SessionCatalogExportDTO | None:
        """原代理已核准入后读取适用镜像；缺页或写后下限不满足交原有界权威路由。"""
        owner = workspace_owner(target)
        self._require_owner(target, owner)
        snapshot = self._snapshots.get(owner)
        projection_changed = snapshot is not None and known_revision is not None and snapshot.revision != known_revision
        minimum = int(query.get("minimum_catalog_revision", "0"))
        if minimum < 0:
            raise ValueError("minimum_catalog_revision 不能为负")
        confirmed_at = self._confirmed_at.get(owner)
        if (snapshot is None or snapshot.catalog_revision < minimum or projection_changed or confirmed_at is None
            or (datetime.now(UTC) - confirmed_at).total_seconds() >= self._refresh_interval_seconds):
            self._request_sync(target, request_id=request_id)
        if snapshot is None or snapshot.catalog_revision < minimum or projection_changed:
            return None
        sync = SessionCatalogSyncDTO(
            gateway_id=owner.gateway_id, workspace_id=owner.workspace_id, source="gateway_mirror",
            stale=owner not in self._confirmed_at or owner in self._in_flight or target.key in self._workspace_errors,
            authoritative_confirmed_at=self._confirmed_at.get(owner), error=self._workspace_errors.get(target.key),
        )
        if path in (["session-catalog", "children"], ["session-catalog", "roots"]):
            parent = query.get("parent_node_id") if path[-1] == "children" else None
            if parent is not None and parent not in snapshot.nodes_by_id:
                return None
            children = snapshot.children_by_parent.get(parent, ())
            limit = int(query.get("limit", "100"))
            if not 1 <= limit <= 500:
                raise ValueError("目录分页 limit 必须位于 1–500")
            offset = decode_catalog_cursor(query.get("cursor"), snapshot.revision, snapshot.catalog_revision, parent, collection_size=len(children))
            next_offset = offset + limit
            return SessionCatalogPageDTO(
                revision=snapshot.revision, catalog_revision=snapshot.catalog_revision, parent_node_id=parent,
                items=[snapshot.nodes_by_id[node_id] for node_id in children[offset:next_offset]], total=len(children),
                cursor=encode_catalog_cursor(next_offset, snapshot.revision, snapshot.catalog_revision, parent) if next_offset < len(children) else None, sync=sync,
            )
        if len(path) == 3 and path[:2] == ["session-catalog", "breadcrumb"]:
            node = snapshot.nodes_by_id.get(path[2])
            if node is None:
                return None
            return SessionCatalogBreadcrumbDTO(
                revision=snapshot.revision, catalog_revision=snapshot.catalog_revision, node_id=node.node_id,
                items=catalog_breadcrumb(node, snapshot.nodes_by_id), sync=sync,
            )
        if path == ["session-catalog", "export"]:
            node_ids = sorted(set(query.get_list("node_id")))
            # 全量导出只能来自权威 owner；不存在的 ID 也不能用旧镜像作否定证明。
            if not node_ids or any(node_id not in snapshot.nodes_by_id for node_id in node_ids):
                return None
            return SessionCatalogExportDTO(
                revision=snapshot.revision, catalog_revision=snapshot.catalog_revision, node_ids=node_ids, complete=False,
                items=[snapshot.nodes_by_id[node_id] for node_id in node_ids], sync=sync,
            )
        raise ValueError("非目录镜像读取范围")

    def search_mirror(
        self, owner: GatewaySearchWorkspaceOwnerDTO, *, query: str, limit: int,
        cursor: str | None, deadline_at: float | None,
    ) -> tuple[SessionCatalogSearchResultsDTO, datetime]:
        """只读已同步的同一 owner 镜像，不触发同步、连接或启动。"""
        snapshot = self._snapshots.get(owner)
        if snapshot is None:
            raise LookupError(f"工作区没有可用目录镜像: {owner.gateway_id}/{owner.workspace_id}")
        page = search_catalog(
            nodes=list(snapshot.nodes_by_id.values()), nodes_by_id=snapshot.nodes_by_id,
            folded_search_terms=snapshot.folded_search_terms,
            revision=snapshot.revision, catalog_revision=snapshot.catalog_revision,
            query=query, limit=limit, cursor=cursor, budget=NameSearchBudget.create(deadline_at),
        )
        return page, snapshot.updated_at

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._sync_all()
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self._refresh_interval_seconds,
                )
            except TimeoutError:
                continue
            self._wake_event.clear()

    async def _sync_all(self) -> None:
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def sync(target: WorkspaceTarget, request_id: str) -> None:
            async with semaphore:
                await self._sync_workspace(
                    target,
                    request_id=request_id,
                )

        targets = self._registry.targets()
        # 会话目录快照/新鲜度/错误表都按 workspace_id 累积，工作区删除后
        # 若只增不减，长驻进程内存会随历史工作区无界增长（快照含全部会话节点）。
        # 每轮同步以权威 registry 存活集为界回收，用集合差而非再扫一遍 targets。
        live_workspace_ids = {target.key for target in targets}
        live_owners = {workspace_owner(target) for target in targets}
        for owner in self._snapshots.keys() - live_owners:
            self._snapshots.pop(owner)
            self._confirmed_at.pop(owner, None)
        for workspace_id in set(self._workspace_errors) - live_workspace_ids:
            self._workspace_errors.pop(workspace_id, None)
        for target in targets:
            if target.connection_kind != "remote_gateway" and not target.backend_url.strip():
                message = f"工作区后端尚未连接: {target.workspace_id}"
                self._workspace_errors[target.key] = message
                logger.info(
                    "跳过尚未连接工作区的会话目录同步: workspace_id=%s",
                    target.workspace_id,
                )
        sync_requests = tuple(
            (target, create_prefixed_id("req"))
            for target in targets
            if target.connection_kind == "remote_gateway"
            or target.backend_url.strip()
        )
        await asyncio.gather(
            *(sync(target, request_id) for target, request_id in sync_requests),
            return_exceptions=True,
        )

    def _require_owner(self, target: WorkspaceTarget, owner: GatewaySearchWorkspaceOwnerDTO) -> None:
        if (not self._registry.has_target(target.key)
            or workspace_owner(self._registry.resolve(target.key)) != owner):
            self._snapshots.pop(owner, None)
            self._confirmed_at.pop(owner, None)
            raise RuntimeError(f"会话目录同步期间登记归属已改变: {target.workspace_id}")

    def _request_sync(self, target: WorkspaceTarget, *, request_id: str) -> asyncio.Task[_CatalogSnapshot]:
        owner = workspace_owner(target)
        pending = self._in_flight.get(owner)
        if pending is not None:
            return pending
        async def sync() -> _CatalogSnapshot:
            try:
                return await self._read_authoritative_version(target, request_id=request_id)
            except Exception as error:
                self._workspace_errors[target.key] = failure_detail(
                    error, stage="session_catalog_sync",
                    label=(f"会话目录同步失败: workspace_id={target.workspace_id}, request_id={request_id}"
                           + (f", status={error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else "")),
                )
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403, 404}:
                    self._snapshots.pop(owner, None)
                    self._confirmed_at.pop(owner, None)
                    await run_sync_shielded(self._cache_path(owner).unlink, missing_ok=True)
                raise
            finally:
                self._in_flight.pop(owner, None)
        task = asyncio.create_task(sync(), name=f"catalog-confirm:{target.workspace_id}")
        # 请求可先返回镜像，后台失败仍写入原错误面并消费 task 异常。
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self._in_flight[owner] = task
        return task

    async def _sync_workspace(self, target: WorkspaceTarget, *, request_id: str) -> _CatalogSnapshot:
        return await asyncio.shield(self._request_sync(target, request_id=request_id))

    async def _read_authoritative_version(self, target: WorkspaceTarget, *, request_id: str) -> _CatalogSnapshot:
        owner = workspace_owner(target)
        self._require_owner(target, owner)
        url, headers = self._target_request(target, request_id=request_id)
        response = await self._http_client.get(url.removesuffix("/export") + "/version", headers=headers, timeout=self._request_timeout_seconds)
        response.raise_for_status()
        version = SessionCatalogVersionDTO.model_validate(response.json()["data"])
        if version.workspace_id != owner.workspace_id:
            raise RuntimeError("会话目录版本响应工作区归属不一致")
        self._require_owner(target, owner)
        cached = self._snapshots.get(owner)
        if cached is not None and cached.revision == catalog_projection_revision(version.catalog_revision, version.projection_revision):
            self._confirmed_at[owner] = datetime.now(UTC)
            self._workspace_errors.pop(target.key, None)
            return cached
        if cached is not None and cached.catalog_revision > version.catalog_revision:
            raise RuntimeError("会话目录权威版本回退")
        response = await self._http_client.get(
            url, headers=headers, params={"minimum_catalog_revision": version.catalog_revision}, timeout=self._request_timeout_seconds,
        )
        response.raise_for_status()
        exported = await run_sync_shielded(lambda: SessionCatalogExportDTO.model_validate(response.json()["data"]))
        if (not exported.complete or exported.catalog_revision < version.catalog_revision
            or exported.sync is None or exported.sync.workspace_id != owner.workspace_id
            or exported.sync.gateway_id != owner.gateway_id or exported.sync.source != "authoritative" or exported.sync.stale):
            raise RuntimeError("会话目录导出未确认目标 owner 的完整权威版本")
        # 目录可在导出前继续推进；同一配置投影的完整更新仍可直接原子发布。
        if exported.revision != catalog_projection_revision(exported.catalog_revision, version.projection_revision):
            raise RuntimeError("会话目录导出期间版本已推进，保留旧镜像并等待下一次确认")
        nodes_by_id = {node.node_id: node for node in exported.items}
        if len(nodes_by_id) != len(exported.items):
            raise RuntimeError(f"工作区目录导出包含重复 node_id: {target.workspace_id}")
        self._require_owner(target, owner)
        snapshot = _CatalogSnapshot(
            owner=owner, catalog_revision=exported.catalog_revision, revision=exported.revision,
            updated_at=datetime.now(UTC), nodes_by_id=nodes_by_id,
        )
        await run_sync_shielded(self._save_snapshot, snapshot)
        self._require_owner(target, owner)
        self._snapshots[owner] = snapshot
        self._confirmed_at[owner] = datetime.now(UTC)
        self._workspace_errors.pop(target.key, None)
        return snapshot

    def _load_snapshot(self, owner: GatewaySearchWorkspaceOwnerDTO) -> _CatalogSnapshot | None:
        path = self._cache_path(owner)
        if not path.exists():
            return None
        payload = read_json_object(path, default={})
        if payload.get("schema_version") != 3:
            raise RuntimeError(f"Gateway 会话目录缓存版本不兼容: owner={owner}")
        if GatewaySearchWorkspaceOwnerDTO.model_validate(payload["owner"]) != owner:
            raise RuntimeError(f"Gateway 会话目录缓存归属不匹配: owner={owner}")
        exported = SessionCatalogExportDTO.model_validate({
            key: payload[key] for key in ("revision", "catalog_revision", "items")
        })
        nodes_by_id = {node.node_id: node for node in exported.items}
        if len(nodes_by_id) != len(exported.items):
            raise RuntimeError(f"Gateway 会话目录索引包含重复 node_id: owner={owner}")
        raw_updated_at = payload.get("updated_at")
        if not isinstance(raw_updated_at, str):
            raise TypeError("Gateway 会话目录缓存缺少更新时间")
        updated_at = datetime.fromisoformat(raw_updated_at)
        if updated_at.tzinfo is None:
            raise ValueError("Gateway 会话目录缓存时间必须包含时区")
        return _CatalogSnapshot(
            owner=owner, catalog_revision=exported.catalog_revision, revision=exported.revision,
            updated_at=updated_at, nodes_by_id=nodes_by_id,
        )

    def _save_snapshot(self, snapshot: _CatalogSnapshot) -> None:
        atomic_write_json(self._cache_path(snapshot.owner), {
            "schema_version": 3, "owner": snapshot.owner.model_dump(),
            "catalog_revision": snapshot.catalog_revision, "revision": snapshot.revision,
            "updated_at": snapshot.updated_at.isoformat(),
            "items": [node.model_dump(mode="json") for node in snapshot.nodes_by_id.values()],
        })

    def _cache_path(self, owner: GatewaySearchWorkspaceOwnerDTO) -> Path:
        digest = hashlib.sha256(f"{owner.gateway_id}\n{owner.workspace_id}".encode()).hexdigest()
        # 旧 route-keyed v2 为可丢弃派生数据；新格式单独归目录，不双读或迁移业务数据。
        return self._cache_dir / "owner-v3" / f"{digest}.json"

    def _target_request(
        self,
        target: WorkspaceTarget,
        *,
        request_id: str,
    ) -> tuple[str, dict[str, str]]:
        headers = self._registry.target_request_headers(target)
        headers["X-BoxTeam-Gateway-Id"] = self._gateway_id
        headers["X-Request-ID"] = request_id
        if target.connection_kind == "remote_gateway":
            assert self._registry.remote_connection_for_target(target).connection_id is not None
            return (
                (f"{self._registry.remote_gateway_url(self._registry.remote_connection_for_target(target).connection_id)}"
                 "/api/v1/session-catalog/export"),
                headers,
            )
        if not target.backend_url.strip():
            raise RuntimeError(f"工作区后端尚未连接: {target.workspace_id}")
        return (
            f"{target.backend_url.rstrip('/')}/api/v1/session-catalog/export",
            headers,
        )
