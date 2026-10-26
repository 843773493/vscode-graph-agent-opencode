"""Workspace 消费真实 Gateway owner 的认证原始 Skill 快照。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import time
from collections.abc import Callable
from contextvars import Context
from dataclasses import dataclass, field

import httpx
from pydantic import ValidationError

from app.core.resource_uri import (
    ParsedResourceUri,
    parse_resource_uri,
    skill_display_uri,
)
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
from app.services.infrastructure.resource_platform.sources.gateway_resource_navigation import (
    send_navigation_request,
    send_navigation_request_sync,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    NAVIGATION_DEADLINE_HEADER,
    ResourceFileOperationError,
    ResourceNavigation,
    navigation_deadline,
    navigation_deadline_at,
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


@dataclass(slots=True)
class _GatewaySkillState:
    gateway_id: str
    snapshot: GatewaySkillSourcesDTO | None = None
    error: str | None = "not-ready"
    etag: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reconcile_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None
    client: httpx.AsyncClient | None = None


class GatewaySkillSources:
    """按真实 Gateway owner 共享来源；请求准备只读取该 owner 的 accepted 事实。"""

    def __init__(
        self, *, workspace_id: str, connection: GatewayTransportConnection,
        local_token_supplier: Callable[[], str], observation_channel: ResourceObservationChannel,
    ) -> None:
        self._workspace_id = workspace_id
        self._connection = connection
        self._local_token_supplier = local_token_supplier
        self._channel = observation_channel
        self._owners: dict[str, _GatewaySkillState] = {}

    def _state(self, gateway_id: str) -> _GatewaySkillState:
        if not gateway_id:
            raise ValueError("Gateway Skill source 必须显式指定真实 owner")
        state = self._owners.get(gateway_id)
        if state is None:
            state = _GatewaySkillState(gateway_id)
            self._owners[gateway_id] = state
        return state

    def directory_id(self, *, gateway_id: str) -> str:
        return "skill-directory:" + hashlib.sha256(f"gateway-skill-sources:{gateway_id}".encode()).hexdigest()

    def snapshot(self, *, gateway_id: str) -> GatewaySkillSourcesDTO:
        state = self._owners.get(gateway_id)
        if state is None or state.error is not None or state.snapshot is None:
            raise RuntimeError(f"Gateway Skill source unavailable: gateway_id={gateway_id}, error={state.error if state is not None else 'not-ready'}")
        return state.snapshot

    def _navigation_request(
        self, request: ResourceNavigationRequest, *, gateway_id: str, deadline: float,
    ) -> tuple[ParsedResourceUri, httpx.Request]:
        parsed = parse_resource_uri(request.uri, local_gateway_id=gateway_id)
        if parsed.gateway_authority is not None or parsed.scope != "gateway" or parsed.scope_id != gateway_id or parsed.kind != "skills":
            raise ResourceUriResolveError("scope_mismatch", "Gateway Skill 导航目标与固定 owner 不符")
        request_id = require_current_request_id("Gateway Skill navigation")
        outgoing = httpx.Request("POST",
            self._connection.resolve_gateway_url()
            + f"/api/gateway/skill-source-connections/{gateway_id}/workspaces/{self._workspace_id}/navigation",
            headers={
                "X-Local-Token": self._local_token_supplier(),
                "X-Request-ID": request_id,
                NAVIGATION_DEADLINE_HEADER: str(navigation_deadline_at(deadline)),
            },
            json=request.model_copy(update={"uri": parsed.display_uri}).model_dump(),
            extensions={"timeout": httpx.Timeout(min(
                self._connection.resolve_timeout_seconds(), max(0, deadline - time.monotonic()),
            )).as_dict()},
        )
        return parsed, outgoing

    def navigate_sync(
        self, request: ResourceNavigationRequest, *, gateway_id: str, deadline: float | None = None,
    ) -> ResourceNavigationResultDTO:
        deadline = navigation_deadline(request, deadline=deadline)
        parsed, outgoing = self._navigation_request(request, gateway_id=gateway_id, deadline=deadline)
        response = send_navigation_request_sync(outgoing, deadline=deadline)
        result = self._navigation_response(response, parsed, outgoing.headers["X-Request-ID"], gateway_id)
        ResourceNavigation._check_deadline(deadline)
        return result

    async def navigate(
        self, request: ResourceNavigationRequest, *, gateway_id: str, deadline: float | None = None,
    ) -> ResourceNavigationResultDTO:
        """只读传输不建立 source owner，也不复用绑定其它事件循环的观察连接。"""
        deadline = navigation_deadline(request, deadline=deadline)
        parsed, outgoing = self._navigation_request(request, gateway_id=gateway_id, deadline=deadline)
        response = await send_navigation_request(outgoing, deadline=deadline)
        result = self._navigation_response(response, parsed, outgoing.headers["X-Request-ID"], gateway_id)
        ResourceNavigation._check_deadline(deadline)
        return result

    @staticmethod
    def _navigation_response(
        response: httpx.Response, parsed: ParsedResourceUri, request_id: str, gateway_id: str,
    ) -> ResourceNavigationResultDTO:
        if response.headers.get("X-Request-ID") != request_id:
            raise ValueError("Gateway Skill navigation 响应头 request_id 与创建请求不一致")
        if response.status_code in {400, 403, 404}:
            detail = response.json().get("detail")
            if isinstance(detail, dict) and detail.get("owner") == "resource_uri_resolver":
                raise ResourceUriResolveError(detail["code"], detail["message"])
            if isinstance(detail, dict) and detail.get("owner") == "resource_file_operations":
                raise ResourceFileOperationError(detail["code"], detail["message"], reason=detail["reason"])
        response.raise_for_status()
        try:
            envelope = APIResponse[ResourceNavigationResultDTO].model_validate_json(response.content)
        except ValidationError as error:
            raise ValueError("Gateway Skill navigation 响应不符合目标 owner 合同") from error
        if envelope.request_id != request_id:
            raise ValueError("Gateway Skill navigation 响应正文 request_id 与创建请求不一致")
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
        require_current_request_id("Gateway Skill source binding")
        state = self._state(gateway_id)
        async with state.lock:
            if state.task is not None:
                if state.task.done():
                    state.task.result()
                    raise RuntimeError("Gateway Skill source observation 意外退出")
                self.snapshot(gateway_id=gateway_id)
                return
            await self.reconcile(gateway_id=gateway_id, wait_for_connection=True)
            self.snapshot(gateway_id=gateway_id)
            # 长驻轮询是独立 HTTP 请求，由 Gateway TraceMiddleware 创建每轮身份。
            state.task = asyncio.create_task(
                self._observe(gateway_id),
                name=f"workspace-gateway-skill-sources:{gateway_id}", context=Context(),
            )

    def _client(self, state: _GatewaySkillState) -> httpx.AsyncClient:
        if state.client is None:
            state.client = httpx.AsyncClient(trust_env=False)
        return state.client

    async def reconcile(
        self, *, gateway_id: str, wait_for_connection: bool = False, independent_poll: bool = False,
    ) -> None:
        """唯一接受入口；仅独立观察轮询由 Gateway 的 HTTP 入口创建请求身份。"""
        state = self._state(gateway_id)
        # 条件请求、验证、接受与错误发布属于同一次 owner 对账，迟到结果不得越过新版。
        async with state.reconcile_lock:
            try:
                request_id = None if independent_poll else require_current_request_id("Gateway Skill source snapshot")
                headers = {"X-Local-Token": self._local_token_supplier()}
                if request_id is not None:
                    headers["X-Request-ID"] = request_id
                if state.etag is not None and state.snapshot is not None:
                    headers["If-None-Match"] = state.etag
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
                            response = await self._client(state).get(
                                self._connection.resolve_gateway_url()
                                + f"/api/gateway/skill-source-connections/{gateway_id}/workspaces/{self._workspace_id}",
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
                if response.status_code != 304:
                    response.raise_for_status()
                response_request_id = response.headers.get("X-Request-ID")
                if not response_request_id or not response_request_id.strip() or (
                    request_id is not None and response_request_id != request_id
                ):
                    raise ValueError("Gateway Skill source 响应头 request_id 无效或与创建请求不一致")
                if response.status_code == 304:
                    if response.content:
                        raise ValueError("Gateway Skill source 304 响应正文必须为空")
                    if state.snapshot is None:
                        raise ValueError("Gateway Skill source 304 缺少 accepted revision")
                    recovered = state.error is not None
                    state.error = None
                    if recovered:
                        self._notify(state)
                    return
                envelope = APIResponse[GatewaySkillSourcesDTO].model_validate_json(response.content)
                if envelope.request_id != response_request_id:
                    raise ValueError("Gateway Skill source 响应正文 request_id 与响应头不一致")
                snapshot = envelope.data
                if envelope.code != 0 or snapshot is None:
                    raise ValueError("Gateway Skill source 缺少有效 data")
                self._accept_snapshot(snapshot, gateway_id=gateway_id)
                state.etag = response.headers.get("ETag")
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as error:
                # 内部日志保留详细原因；公开观察只发布 owner 可用性，不泄漏凭据或路径。
                state.error = type(error).__name__
                self._notify(state)
                logger.error("Gateway Skill source 对账失败: gateway_id=%s error=%s", gateway_id, error)

    def _accept_snapshot(self, snapshot: GatewaySkillSourcesDTO, *, gateway_id: str) -> None:
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
        state = self._state(gateway_id)
        changed = snapshot != state.snapshot or state.error is not None
        state.snapshot = snapshot
        state.error = None
        if changed:
            self._notify(state)

    def _notify(self, state: _GatewaySkillState) -> None:
        self._channel.notify(ResourceObservation(
            uri=None, directory_id=self.directory_id(gateway_id=state.gateway_id),
            revision=state.snapshot.revision if state.snapshot is not None else None,
            available=state.error is None and state.snapshot is not None and state.snapshot.available,
        ))

    async def _observe(self, gateway_id: str) -> None:
        while True:
            await asyncio.sleep(1)
            await self.reconcile(gateway_id=gateway_id, independent_poll=True)

    async def close(self) -> None:
        tasks = [state.task for state in self._owners.values() if state.task is not None]
        for state in self._owners.values():
            state.task = None
        for task in tasks:
            task.cancel()
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures = [result for result in results if isinstance(result, Exception)]
            if failures:
                raise ExceptionGroup("Gateway Skill source observation 清理失败", failures)
        finally:
            await asyncio.gather(*(state.client.aclose() for state in self._owners.values() if state.client is not None))
