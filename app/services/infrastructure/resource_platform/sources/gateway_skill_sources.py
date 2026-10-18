"""Workspace 消费真实 Gateway owner 的认证原始 Skill 快照。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
from collections.abc import Callable

import httpx

from app.core.resource_uri import parse_resource_uri, skill_display_uri
from app.core.trace_middleware import require_current_request_id
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.gateway.skill_sources import GatewaySkillSourcesDTO
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservation,
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)

logger = logging.getLogger(__name__)


def _connection_refused(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, OSError) and cause.errno == errno.ECONNREFUSED:
            return True
        cause = cause.__cause__ or cause.__context__
    return False


class GatewaySkillSources:
    """每个 Workspace 进程共享一次连接；请求准备只读取 accepted 内存事实。"""

    def __init__(
        self, *, workspace_id: str, connection: GatewayTransportConnection,
        local_token_supplier: Callable[[], str], observation_channel: ResourceObservationChannel,
    ) -> None:
        self._workspace_id = workspace_id
        self._connection = connection
        self._local_token_supplier = local_token_supplier
        self._channel = observation_channel
        self._gateway_id: str | None = None
        self._snapshot: GatewaySkillSourcesDTO | None = None
        self._error: str | None = "not-ready"
        self._etag: str | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def gateway_id(self) -> str:
        if self._gateway_id is None:
            raise RuntimeError("Gateway Skill source 尚未绑定真实 Gateway 身份")
        return self._gateway_id

    @property
    def directory_id(self) -> str:
        if self._gateway_id is None:
            raise RuntimeError("Gateway Skill source 尚未绑定真实 Gateway 身份")
        return "skill-directory:" + hashlib.sha256(f"gateway-skill-sources:{self._gateway_id}".encode()).hexdigest()

    def snapshot(self, *, gateway_id: str) -> GatewaySkillSourcesDTO:
        if gateway_id != self._gateway_id:
            raise RuntimeError("Gateway Skill source owner mismatch")
        if self._error is not None or self._snapshot is None:
            raise RuntimeError(f"Gateway Skill source unavailable: {self._error}")
        return self._snapshot

    async def navigate(
        self, request: ResourceNavigationRequest, *, gateway_id: str,
    ) -> ResourceNavigationResultDTO:
        """普通读取走固定 Gateway 的认证 owner，不绑定来源或启动快照观察。"""
        parsed = parse_resource_uri(request.uri, local_gateway_id=gateway_id)
        if parsed.gateway_authority is not None or parsed.scope != "gateway" or parsed.scope_id != gateway_id or parsed.kind != "skills":
            raise ResourceUriResolveError("scope_mismatch", "Gateway Skill 导航目标与固定 owner 不符")
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                self._connection.resolve_gateway_url()
                + f"/api/gateway/skill-sources/{gateway_id}/workspaces/{self._workspace_id}/navigation",
                headers={
                    "X-Local-Token": self._local_token_supplier(),
                    "X-Request-ID": require_current_request_id("Gateway Skill navigation"),
                },
                json=request.model_copy(update={"uri": parsed.display_uri}).model_dump(),
                timeout=self._connection.resolve_timeout_seconds(),
            )
        if response.status_code in {400, 403, 404}:
            detail = response.json().get("detail")
            if isinstance(detail, dict) and detail.get("owner") == "resource_uri_resolver":
                raise ResourceUriResolveError(detail["code"], detail["message"])
        response.raise_for_status()
        envelope = APIResponse[ResourceNavigationResultDTO].model_validate_json(response.content)
        result = envelope.data
        if envelope.code != 0 or result is None or result.node.uri != parsed.display_uri:
            raise ValueError("Gateway Skill navigation 响应目标不一致")
        for node in (result.node, *result.entries):
            child = parse_resource_uri(node.uri, local_gateway_id=gateway_id)
            if (
                child.gateway_authority is not None or child.scope != parsed.scope
                or child.scope_id != parsed.scope_id or child.kind != parsed.kind
                or child.tail_segments[:len(parsed.tail_segments)] != parsed.tail_segments
            ):
                raise ValueError("Gateway Skill navigation 返回了另一根的节点")
        return result

    async def ensure_bound(self, gateway_id: str) -> None:
        """真实 runtime 编译前建立来源；不参与 prepare 或 skill_load。"""
        async with self._lock:
            if self._gateway_id is not None and self._gateway_id != gateway_id:
                raise RuntimeError("Workspace 已绑定另一 Gateway Skill source owner")
            if self._task is not None:
                if self._task.done():
                    self._task.result()
                    raise RuntimeError("Gateway Skill source observation 意外退出")
                self.snapshot(gateway_id=gateway_id)
                return
            self._gateway_id = gateway_id
            await self.reconcile(wait_for_connection=True)
            self.snapshot(gateway_id=gateway_id)
            self._task = asyncio.create_task(self._observe(), name="workspace-gateway-skill-sources")

    async def reconcile(self, *, wait_for_connection: bool = False) -> None:
        """认证响应校验是唯一接受远端正文与 revision 的入口。"""
        gateway_id = self._gateway_id
        if gateway_id is None:
            raise RuntimeError("Gateway Skill source 未绑定 owner")
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(trust_env=False)
            headers = {"X-Local-Token": self._local_token_supplier()}
            if self._etag is not None and self._snapshot is not None:
                headers["If-None-Match"] = self._etag
            timeout = self._connection.resolve_timeout_seconds()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            # HTTPX 的 timeout 按网络阶段计时；首次绑定还须共享总墙钟预算。
            async with asyncio.timeout_at(deadline if wait_for_connection else None):
                while True:
                    remaining = deadline - loop.time() if wait_for_connection else timeout
                    if wait_for_connection and remaining <= 0:
                        raise TimeoutError("Gateway Skill source 首次绑定超时")
                    try:
                        response = await self._client.get(
                            self._connection.resolve_gateway_url()
                            + f"/api/gateway/skill-sources/{gateway_id}/workspaces/{self._workspace_id}",
                            headers=headers,
                            timeout=remaining,
                        )
                    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                        # 冷启动只等连接拒绝或建立超时；DNS、网络、TLS 等错误立即失败。
                        if not wait_for_connection or not (
                            isinstance(error, httpx.ConnectTimeout) or _connection_refused(error)
                        ):
                            raise
                        await asyncio.sleep(min(0.1, max(deadline - loop.time(), 0)))
                        continue
                    break
            if response.status_code == 304:
                if self._snapshot is None:
                    raise ValueError("Gateway Skill source 304 缺少 accepted revision")
                recovered = self._error is not None
                self._error = None
                if recovered:
                    self._notify()
                return
            response.raise_for_status()
            envelope = APIResponse[GatewaySkillSourcesDTO].model_validate_json(response.content)
            snapshot = envelope.data
            if envelope.code != 0 or snapshot is None:
                raise ValueError("Gateway Skill source 缺少有效 data")
            self._accept_snapshot(snapshot)
            self._etag = response.headers.get("ETag")
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as error:
            # 内部日志保留详细原因；公开观察只发布 owner 可用性，不泄漏凭据或路径。
            self._error = type(error).__name__
            self._notify()
            logger.error("Gateway Skill source 对账失败: gateway_id=%s error=%s", gateway_id, error)

    def _accept_snapshot(self, snapshot: GatewaySkillSourcesDTO) -> None:
        gateway_id = self.gateway_id
        if snapshot.gateway_id != gateway_id or snapshot.workspace_id != self._workspace_id:
            raise ValueError("Gateway Skill source owner/audience mismatch")
        names: set[str] = set()
        for entry in snapshot.entries:
            if entry.name in names or entry.name in {".", ".."} or "/" in entry.name or "\\" in entry.name:
                raise ValueError("Gateway Skill source entry identity 无效")
            names.add(entry.name)
            if (
                entry.entry_identity != f"skill-entry:gateway:{gateway_id}:{entry.name}"
                or entry.source_id != f"gateway-skill-source:{gateway_id}:{entry.name}"
                or entry.display_uri != skill_display_uri(scope="gateway", scope_id=gateway_id, skill_name=entry.name)
            ):
                raise ValueError("Gateway Skill source entry provenance mismatch")
            raw = entry.content.encode("utf-8")
            if len(raw) != entry.byte_length or (
                entry.available and entry.revision != "sha256:" + hashlib.sha256(raw).hexdigest()
            ):
                raise ValueError("Gateway Skill source byte revision mismatch")
        expected_revision = "sha256:" + hashlib.sha256(canonical_json_bytes({
            "gateway_id": gateway_id, "available": snapshot.available,
            "entries": [(entry.source_id, entry.revision, entry.available) for entry in snapshot.entries],
        })).hexdigest()
        if snapshot.revision != expected_revision:
            raise ValueError("Gateway Skill source publication revision mismatch")
        changed = snapshot != self._snapshot or self._error is not None
        self._snapshot = snapshot
        self._error = None
        if changed:
            self._notify()

    def _notify(self) -> None:
        self._channel.notify(ResourceObservation(
            uri=None, directory_id=self.directory_id,
            revision=self._snapshot.revision if self._snapshot is not None else None,
            available=self._error is None and self._snapshot is not None and self._snapshot.available,
        ))

    async def _observe(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.reconcile()

    async def close(self) -> None:
        task, self._task = self._task, None
        try:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            if self._client is not None:
                await self._client.aclose()
