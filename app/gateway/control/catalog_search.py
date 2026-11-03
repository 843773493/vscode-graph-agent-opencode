from __future__ import annotations

import asyncio
import hashlib
import logging
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path

import httpx

from app.core.failures import failure_detail
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_gateway_root
from app.gateway.auth import LOCAL_TOKEN
from app.gateway.control.storage import atomic_write_json, read_json_object
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.registry.owners import workspace_owner
from app.gateway.runtime.consumer_protocol import GatewayRuntimeHealthProof
from app.schemas.gateway.search import GatewaySearchWorkspaceOwnerDTO
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogExportDTO,
    SessionCatalogNodeDTO,
    SessionCatalogSearchResultsDTO,
)
from app.services.business.session_navigation.catalog_query import search_catalog
from app.services.business.session_navigation.name_search_budget import NameSearchBudget
from app.services.mapping.session_catalog_search import (
    CatalogSearchTerms,
    catalog_search_terms,
)

logger = logging.getLogger(__name__)

# 分片锁池：同一 workspace_id 恒得同一把锁（保住同步互斥），池大小恒定，
# 工作区被删除后也不会留下永不回收的锁对象。
_SYNC_LOCK_SHARDS = 64


@dataclass(frozen=True)
class _CatalogSnapshot:
    owner: GatewaySearchWorkspaceOwnerDTO
    catalog_revision: int
    revision: str
    updated_at: datetime
    nodes_by_id: dict[str, SessionCatalogNodeDTO]

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
        self._workspace_errors: dict[str, str] = {}
        self._sync_locks: tuple[asyncio.Lock, ...] = tuple(
            asyncio.Lock() for _ in range(_SYNC_LOCK_SHARDS)
        )
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
            owner = workspace_owner(target, self._registry, self._gateway_id)
            snapshot = self._load_snapshot(owner)
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
        live_workspace_ids = {target.workspace_id for target in targets}
        live_owners = {workspace_owner(target, self._registry, self._gateway_id) for target in targets}
        for owner in self._snapshots.keys() - live_owners:
            self._snapshots.pop(owner)
        for workspace_id in set(self._workspace_errors) - live_workspace_ids:
            self._workspace_errors.pop(workspace_id, None)
        for target in targets:
            if target.connection_kind != "remote_gateway" and not target.backend_url.strip():
                message = f"工作区后端尚未连接: {target.workspace_id}"
                self._workspace_errors[target.workspace_id] = message
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
        results = await asyncio.gather(
            *(sync(target, request_id) for target, request_id in sync_requests),
            return_exceptions=True,
        )
        for (target, request_id), result in zip(sync_requests, results, strict=True):
            if isinstance(result, BaseException):
                self._workspace_errors[target.workspace_id] = failure_detail(
                    result, stage="session_catalog_sync",
                    label=(f"会话目录同步失败: workspace_id={target.workspace_id}, request_id={request_id}"
                           + (f", status={result.response.status_code}" if isinstance(result, httpx.HTTPStatusError) else "")),
                )

    async def _sync_workspace(
        self,
        target: WorkspaceTarget,
        *,
        request_id: str,
    ) -> _CatalogSnapshot:
        lock = self._sync_lock_for(target.workspace_id)
        async with lock:
            owner = workspace_owner(target, self._registry, self._gateway_id)
            url, headers = self._target_request(target, request_id=request_id)
            response = await self._http_client.get(
                url,
                headers=headers,
                timeout=self._request_timeout_seconds,
            )
            response.raise_for_status()
            exported = SessionCatalogExportDTO.model_validate(response.json()["data"])
            nodes_by_id = {node.node_id: node for node in exported.items}
            if len(nodes_by_id) != len(exported.items):
                raise RuntimeError(f"工作区目录导出包含重复 node_id: {target.workspace_id}")
            if (not self._registry.has_target(target.workspace_id)
                or workspace_owner(self._registry.resolve(target.workspace_id), self._registry, self._gateway_id) != owner):
                raise RuntimeError(f"会话目录同步期间登记归属已改变: {target.workspace_id}")
            snapshot = _CatalogSnapshot(
                owner=owner, catalog_revision=exported.catalog_revision, revision=exported.revision,
                updated_at=datetime.now(UTC), nodes_by_id=nodes_by_id,
            )
            self._save_snapshot(snapshot)
            self._snapshots[owner] = snapshot
            self._workspace_errors.pop(target.workspace_id, None)
            return snapshot

    def _sync_lock_for(self, workspace_id: str) -> asyncio.Lock:
        # 分片锁池：同一 workspace_id 恒得同一把锁（保住同一工作区同步互斥），
        # 池大小恒定（有界）。用 crc32 而非 hash() 是为了跨进程确定，不引入
        # hash 随机化导致的锁分布漂移。
        shard = zlib.crc32(workspace_id.encode("utf-8")) % len(self._sync_locks)
        return self._sync_locks[shard]

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
                (f"{self._registry.remote_gateway_url(target.remote_gateway_connection_id)}"
                 "/api/v1/session-catalog/export"),
                {
                    "X-BoxTeam-Workspace-Id": target.remote_workspace_id,
                    "X-BoxTeam-Federation-Token": credential.token,
                    "X-BoxTeam-Gateway-Id": self._gateway_id,
                    "X-Request-ID": request_id,
                },
            )
        if not target.backend_url.strip():
            raise RuntimeError(f"工作区后端尚未连接: {target.workspace_id}")
        if target.backend_workspace_id is None:
            raise RuntimeError(
                f"工作区后端缺少 backend UUID: {target.workspace_id}"
            )
        return (
            f"{target.backend_url.rstrip('/')}/api/v1/session-catalog/export",
            {
                "X-Local-Token": LOCAL_TOKEN,
                "X-BoxTeam-Workspace-Id": target.backend_workspace_id,
                "X-BoxTeam-Gateway-Id": self._gateway_id,
                "X-Request-ID": request_id,
            },
        )
