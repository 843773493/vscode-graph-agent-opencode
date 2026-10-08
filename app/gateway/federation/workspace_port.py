"""联邦 workspace HTTP port：复用权威 main endpoint，透明传输 send/read/wait。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import httpx

from app.core.trace_middleware import (
    require_current_gateway_id,
    require_current_request_id,
)
from app.gateway.auth import LOCAL_TOKEN
from app.gateway.federation.errors import (
    FEDERATION_DEADLINE_EXCEEDED,
    FEDERATION_SESSION_MAIN_UNAVAILABLE,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FederationError,
)
from app.gateway.federation.rpc import FederationSpoke
from app.gateway.proxy_upstream import load_proxy_gateway_id
from app.schemas.gateway import GatewayWorkspaceListDTO
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.services.business.gateway_context_query_service import (
    GatewayContextQueryService,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    RESOURCE_URI_RESOLVE_REASON_CODES,
)

logger = logging.getLogger(__name__)

#: 复用工作区后端已提供的权威 main pointer 合同路径。
SESSION_MAIN_THREAD_ENDPOINT = "/api/v1/sessions/{session_id}/main-thread"


class FederationSpokeDirectory:
    """由组合根注入的 active spoke 目录；只暴露稳定身份，不泄露 credential。"""

    def __init__(self, *, provider: Callable[[], tuple[FederationSpoke, ...]]) -> None:
        self._provider = provider

    def active_spokes(self) -> tuple[FederationSpoke, ...]:
        return self._provider()


class WorkspaceCatalogPort:
    """按 backend_url 只读查询各本地工作区的 cold session catalog。"""

    def __init__(self, *, request_timeout: float = 5.0) -> None:
        self._request_timeout = request_timeout
        self._backend_endpoints: dict[str, tuple[str, str]] = {}

    def project_workspaces(
        self,
        workspaces: tuple[tuple[str, str, str], ...],
    ) -> None:
        """整体重建本地工作区路由表；远端子工作区不参与。"""

        self._backend_endpoints = {
            workspace_id: (backend_url.rstrip("/"), backend_workspace_id)
            for workspace_id, backend_url, backend_workspace_id in workspaces
        }

    async def _export(self, *, workspace_id: str) -> dict[str, object]:
        request_id = require_current_request_id("联邦 cold catalog")
        backend_endpoint = self._backend_endpoints.get(workspace_id)
        if backend_endpoint is None:
            raise FederationError(
                FEDERATION_SESSION_MAIN_UNAVAILABLE,
                f"本地工作区后端未连接，无法查询 cold catalog: {workspace_id}",
                detail={"workspace_id": workspace_id},
            )
        backend_url, backend_workspace_id = backend_endpoint
        async with httpx.AsyncClient(timeout=self._request_timeout, trust_env=False) as client:
            response = await client.get(
                f"{backend_url}/api/v1/session-catalog/export",
                headers={
                    "X-Local-Token": LOCAL_TOKEN,
                    "X-BoxTeam-Workspace-Id": backend_workspace_id,
                    "X-Request-ID": request_id,
                    # 按请求注入 Gateway 自身稳定身份，取值与两条 HTTP 代理同源。
                    "X-BoxTeam-Gateway-Id": load_proxy_gateway_id(),
                },
            )
        if response.status_code >= 400:
            raise FederationError(
                FEDERATION_SESSION_MAIN_UNAVAILABLE,
                f"工作区 cold catalog 查询失败: status={response.status_code}",
                detail={"workspace_id": workspace_id},
            )
        try:
            body = response.json()
        except json.JSONDecodeError as error:
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, f"cold catalog 响应不是 JSON: {response.text[:2000]}") from error
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("request_id") != request_id or response.headers.get("X-Request-ID") != request_id:
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, "cold catalog 响应 request_id 漂移")
        if not isinstance(data, dict):
            raise FederationError(
                FEDERATION_SESSION_MAIN_UNAVAILABLE,
                "工作区 cold catalog 导出响应缺少 data",
            )
        return data

    async def local_session_workspaces(self, session_id: str) -> tuple[str, ...]:
        """返回包含该 session 的本地工作区；未授权与不存在不可区分。"""

        found: list[str] = []
        for workspace_id in sorted(self._backend_endpoints):
            try:
                data = await self._export(workspace_id=workspace_id)
            except FederationError:
                raise
            except Exception as error:  # 不可达即显式失败，不静默
                raise FederationError(
                    FEDERATION_SESSION_MAIN_UNAVAILABLE,
                    f"本地工作区 cold catalog 不可达: {workspace_id}: {error}",
                ) from error
            items = data.get("items")
            if not isinstance(items, list):
                raise FederationError(
                    FEDERATION_SESSION_MAIN_UNAVAILABLE,
                    "工作区 cold catalog 导出缺少 items 数组",
                )
            if any(
                isinstance(item, dict) and item.get("session_id") == session_id
                for item in items
            ):
                found.append(self._backend_endpoints[workspace_id][1])
        return tuple(found)


class WorkspaceSessionMainPort:
    """只调用注册工作区的权威 HTTP 端点，不读业务盘或创建 Job。"""

    def __init__(
        self, *, request_timeout: float = 5.0,
        inventory_provider: Callable[[], Awaitable[GatewayWorkspaceListDTO]] | None = None,
    ) -> None:
        self._request_timeout = request_timeout
        self._inventory_provider = inventory_provider
        self._context_query = GatewayContextQueryService(transport=self)
        self._backend_urls: dict[str, str] = {}
        self._backend_workspace_ids: dict[str, str] = {}

    def project_workspaces(self, workspaces: tuple[tuple[str, str, str], ...]) -> None:
        projected = {backend_id: url.rstrip("/") for _route_id, url, backend_id in workspaces}
        if len(projected) != len(workspaces):
            raise RuntimeError("多个本地 Gateway route 指向同一稳定 workspace identity")
        self._backend_urls = projected
        self._backend_workspace_ids = {route_id: backend_id for route_id, _url, backend_id in workspaces}

    def require_local_source(self, gateway_id: str, source: dict[str, object]) -> None:
        if source.get("gateway_id") != gateway_id or source.get("workspace_id") not in self._backend_urls:
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, "origin 不属于当前 Gateway 注册的本地 workspace")

    def canonical_workspace_id(self, workspace_id: str) -> str:
        return self._backend_workspace_ids.get(workspace_id, workspace_id)

    async def _request(self, *, workspace_id: str, method: str, path: str, origin: str, body: dict[str, object] | None = None, timeout: float | None = None) -> dict[str, object]:
        request_id = require_current_request_id("联邦 workspace HTTP port")
        url = self._backend_urls.get(workspace_id)
        if url is None:
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, f"本地 workspace 不可达: {workspace_id}")
        async with httpx.AsyncClient(timeout=timeout if timeout is not None else self._request_timeout, trust_env=False) as client:
            response = await client.request(method, url + path, json=body, headers={
                "X-Local-Token": LOCAL_TOKEN, "X-BoxTeam-Workspace-Id": workspace_id,
                "X-BoxTeam-Gateway-Id": load_proxy_gateway_id(), "X-Request-ID": request_id,
                "X-BoxTeam-Communication-Origin": origin,
            })
        if response.status_code >= 400:
            from app.core.session_control_communication_ledger.observations import (
                observation_error_code,
            )
            if path in {"/api/v1/context/read", "/api/v1/context/search"}:
                if response.status_code in {403, 404}:
                    raise FederationError(FEDERATION_TARGET_NOT_RESOLVABLE, "上下文资源未共享或不存在")
                if response.headers.get("content-type", "").startswith("application/json"):
                    payload = response.json()
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    code = detail.get("code") if isinstance(detail, dict) else None
                    if isinstance(code, str) and (
                        code in RESOURCE_URI_RESOLVE_REASON_CODES or code == "snapshot_changed"
                    ):
                        raise FederationError(code, code)
                raise FederationError(
                    FEDERATION_SESSION_MAIN_UNAVAILABLE,
                    f"工作区上下文 HTTP 失败: status={response.status_code}",
                )
            if response.headers.get("content-type", "").startswith("application/json"):
                code = observation_error_code(response.json())
                if code is not None:
                    raise FederationError(code, code)
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, f"工作区通信 HTTP 失败: status={response.status_code}, detail={response.text[:2000]}")
        try:
            data = response.json()
        except json.JSONDecodeError as error:
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, f"工作区通信响应不是 JSON: {response.text[:2000]}") from error
        if not isinstance(data, dict) or data.get("request_id") != request_id or response.headers.get("X-Request-ID") != request_id or not isinstance(data.get("data"), dict):
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, "工作区通信响应身份或 data 非法")
        return data["data"]

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO:
        if self._inventory_provider is None:
            raise RuntimeError("Gateway inventory provider 尚未装配")
        inventory = await self._inventory_provider()
        # 联邦目标只执行自己拥有的资源；不经 registry 的远端投影继续第四跳。
        return inventory.model_copy(update={
            "items": [item for item in inventory.items if item.connection_kind == "local"],
        })

    async def read_context_in_workspace(
        self, workspace_id: str, request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        data = await self._request(
            workspace_id=self.canonical_workspace_id(workspace_id), method="POST",
            path="/api/v1/context/read", origin=require_current_gateway_id("read context target"),
            body=request.model_dump(mode="json"),
        )
        return SessionContextReadResultDTO.model_validate(data)

    async def search_context_in_workspace(
        self, workspace_id: str, request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        data = await self._request(
            workspace_id=self.canonical_workspace_id(workspace_id), method="POST",
            path="/api/v1/context/search", origin=require_current_gateway_id("search context target"),
            body=request.model_dump(mode="json"),
        )
        return SessionContextSearchResultDTO.model_validate(data)

    async def resolve_main_thread(self, *, gateway_id: str, workspace_id: str, session_id: str) -> str:
        from app.core.session_catalog_store import (
            validate_session_id,
            validate_thread_id,
        )
        validate_session_id(session_id)
        data = await self._request(workspace_id=workspace_id, method="GET", path=SESSION_MAIN_THREAD_ENDPOINT.format(session_id=session_id), origin=gateway_id)
        if data.get("session_id") != session_id:
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, "main-thread 响应 Session 身份漂移")
        thread_id = data.get("thread_id")
        validate_thread_id(thread_id)
        return thread_id

    async def execute(self, *, operation: str, target: dict[str, object], body: dict[str, object], origin: str, timeout: float) -> dict[str, object]:
        if "resource_identity" in target:
            from app.schemas.internal_v2.communication import ContextQueryRequest
            from app.services.business.session_context_resource import (
                parse_session_context_ref,
            )
            query = ContextQueryRequest.model_validate(body)
            if operation != "read" or query.resource_target.to_payload() != target or query.source_gateway_id != origin:
                raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, "上下文查询与 read grant target/origin 不一致")
            resource = parse_session_context_ref(
                resource_identity=query.resource_target.resource_identity,
                uri=query.resource_target.uri, scope=query.resource_target.scope,
                local_gateway_id=require_current_gateway_id("context target"),
            )
            if resource.gateway_authority is not None:
                raise FederationError(FEDERATION_TARGET_NOT_RESOLVABLE, "上下文 URI 不属于当前 Gateway")
            if resource.scope == "workspace" and resource.scope_id not in self._backend_urls:
                raise FederationError(FEDERATION_TARGET_NOT_RESOLVABLE, "上下文 workspace 未共享或不存在")
            try:
                async with asyncio.timeout(timeout):
                    if resource.scope == "gateway":
                        result = await self._context_query.read_gateway_context(query.read) if query.read is not None else await self._context_query.search_gateway_context(query.search)
                    elif query.read is not None:
                        result = await self.read_context_in_workspace(resource.scope_id, query.read)
                    else:
                        result = await self.search_context_in_workspace(resource.scope_id, query.search)
            except TimeoutError as error:
                raise FederationError(FEDERATION_DEADLINE_EXCEEDED, "上下文查询超过联邦总 deadline") from error
            return result.model_dump(mode="json")
        if operation in {"send", "reply"}:
            from app.schemas.internal_v2.communication import (
                CommunicationAcceptanceRequest,
            )
            accepted = CommunicationAcceptanceRequest.model_validate(body)
            if (accepted.payload.kind == "reply") != (operation == "reply"):
                raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, "relay operation 与 payload kind 不一致")
        else:
            from app.schemas.internal_v2.communication import (
                CommunicationObservationRequest,
            )
            observed = CommunicationObservationRequest.model_validate(body)
            if observed.operation != operation:
                raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, "relay operation 与 observation 不一致")
        source = body.get("source")
        if not isinstance(source, dict) or source.get("gateway_id") != origin:
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, "通信 source 与认证 channel origin 不一致")
        if body.get("target") != target:
            raise FederationError(FEDERATION_SESSION_MAIN_UNAVAILABLE, "通信 body target 与冻结 target main 不一致")
        session_id = target["session_id"]
        path = f"/api/v1/sessions/{session_id}/" + ("inter-agent-messages" if operation in {"send", "reply"} else "communication-observations")
        return await self._request(workspace_id=target["workspace_id"], method="POST", path=path, origin=origin, body=body, timeout=timeout)


__all__ = ["LOCAL_TOKEN", "SESSION_MAIN_THREAD_ENDPOINT", "FederationSpokeDirectory", "WorkspaceCatalogPort", "WorkspaceSessionMainPort"]
