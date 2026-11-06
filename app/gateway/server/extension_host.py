"""独立认证宿主通道；Gateway 只冻结路由、判权和转发原协议。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import ValidationError
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    get_gateway_local_token,
    verify_gateway_access,
)
from app.gateway.auxiliary_proxy import _websocket_target
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessContext,
    UserAccessService,
)
from app.gateway.federation.policy import (
    FederationPolicyStore,
    require_federation_capability,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceRouteLease
from app.gateway.server.workspace_proxy import (
    _http_client,
    verify_user_access_for_proxy,
)
from app.schemas.gateway.extension_host import (
    ExtensionHostConnectionDTO,
    ExtensionHostOpenRequest,
    ExtensionHostTargetDTO,
    ExtensionHostTargetRequest,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.extension_host.channel import (
    close_channel,
    run_channel,
)

router = APIRouter()
logger = logging.getLogger(__name__)
CLIENT_HEADER = "X-BoxTeam-Extension-Client-Id"


def _access(websocket: WebSocket) -> tuple[GatewayAuthContext, UserAccessContext | None, str]:
    auth = verify_gateway_access(
        x_local_token=websocket.headers.get("x-local-token") or websocket.query_params.get("token"),
        x_boxteam_federation_token=websocket.headers.get("x-boxteam-federation-token"))
    require_federation_capability(websocket, auth, "extension_host_manage")
    if auth.kind == "federation":
        client_id = websocket.headers.get(CLIENT_HEADER)
        if not client_id or len(client_id) > 256 or not client_id.isascii():
            raise HTTPException(400, "联邦宿主通道缺少原始客户端绑定")
        # 远端只能在本跳已认证 peer 的命名空间中声明客户端，不能覆盖原生会话。
        return auth, None, f"peer:{auth.peer_gateway_id}:{client_id}"
    service = _users(websocket)
    access = service.resolve_cookie(websocket.cookies.get(USER_ACCESS_COOKIE_NAME))
    if access is None:
        raise HTTPException(401, "user_session_required")
    # cookie 是访问凭据，只派生不含凭据正文的客户端身份；guest 同样沿既有租约。
    identity = f"{access.kind}:{access.user_id}:{access.access_session_id}:{access.lease_generation}"
    return auth, access, hashlib.sha256(identity.encode()).hexdigest()


def _users(websocket: HTTPConnection) -> UserAccessService:
    service = getattr(websocket.app.state, "user_access_service", None)
    if not isinstance(service, UserAccessService):
        raise TypeError("Gateway 用户访问服务尚未初始化")
    return service


def _check(websocket: HTTPConnection, auth: GatewayAuthContext, access: UserAccessContext | None,
           leases: list[WorkspaceRouteLease]) -> None:
    require_federation_capability(websocket, auth, "extension_host_manage")
    if access is not None and access.invalidated.is_set():
        raise PermissionError("Node 宿主客户端租约已失效")
    if any(lease.invalidated.is_set() for lease in leases):
        raise LookupError("Node 宿主工作区路由已失效")


@dataclass(frozen=True, slots=True)
class _HostRoute:
    base_url: str
    remote: bool
    headers: dict[str, str]
    leases: list[WorkspaceRouteLease]


@contextmanager
def _target_route(connection: HTTPConnection, *, gateway_id: str, workspace_ids: list[str],
                  auth: GatewayAuthContext, streaming: bool) -> Iterator[_HostRoute]:
    registry = getattr(connection.app.state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise TypeError("Gateway 工作区注册表尚未初始化")
    targets = [registry.resolve((gateway_id, workspace_id)) for workspace_id in workspace_ids]
    if auth.kind == "federation" and any(target.connection_kind != "local" for target in targets):
        raise PermissionError("联邦宿主通道禁止嵌套远端转发")
    with ExitStack() as references:
        leases = []
        for target in targets:
            if target.connection_error is not None:
                raise LookupError(f"Node 宿主工作区连接不可用: {target.connection_error}")
            leases.append(registry.acquire_route_reference(target.key, streaming=streaming))
            references.callback(registry.release_route_reference, target.key, streaming=streaming)
        urls = {registry.resolve_service_url(target.key, "workspace_api") for target in targets}
        if len(urls) != 1 or len({target.connection_kind for target in targets}) != 1:
            raise ValueError("Node 宿主视图必须属于同一 Backend 目标")
        headers = {"X-Request-ID": get_request_id(connection)}
        remote = targets[0].connection_kind == "remote_gateway"
        if remote:
            if len({registry.remote_connection_for_target(target).connection_id for target in targets}) != 1:
                raise ValueError("Node 宿主视图必须使用同一明确远端连接")
            headers.update(registry.target_request_headers(targets[0]))
        else:
            if gateway_id != registry.gateway_id:
                raise ValueError("Node 宿主目标不属于原生 Gateway")
            headers.update({"X-Local-Token": get_gateway_local_token(), "X-BoxTeam-Gateway-Id": registry.gateway_id})
        yield _HostRoute(urls.pop(), remote, headers, leases)


@router.post("/api/gateway/extension-hosts/resolve", response_model=APIResponse[ExtensionHostTargetDTO])
async def resolve_extension_host(
    payload: ExtensionHostTargetRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionHostTargetDTO] | Response:
    require_federation_capability(request, auth, "extension_host_manage")
    try:
        with _target_route(request, gateway_id=payload.gateway_id, workspace_ids=payload.workspace_ids,
                           auth=auth, streaming=False) as route:
            _check(request, auth, access, route.leases)
            path = "api/gateway/extension-hosts/resolve" if route.remote else "api/internal/extension-hosts/resolve"
            response = await _http_client(request).post(f"{route.base_url.rstrip('/')}/{path}",
                headers=route.headers, json=payload.model_dump(), timeout=10)
            _check(request, auth, access, route.leases)
            if access is not None:
                expires_at = _users(request).expires_at(access)
                if expires_at is None or datetime.fromisoformat(expires_at) <= datetime.now(UTC):
                    raise PermissionError("Node 宿主客户端租约已过期")
            if not response.is_success:
                return Response(response.content, status_code=response.status_code, media_type="application/json")
            result = APIResponse[ExtensionHostTargetDTO].model_validate_json(response.content)
            target = result.data
            if (result.code != 0 or result.request_id != request_id or response.headers.get("X-Request-ID") != request_id
                or target is None or target.gateway_id != payload.gateway_id or target.workspace_ids != payload.workspace_ids
                or target.protocol_version != payload.protocol_version
                or (payload.backend_id is not None and target.backend_id != payload.backend_id)):
                raise ValueError("Node 宿主返回了不同目标或请求身份")
            return result
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except (httpx.RequestError, ValidationError) as error:
        raise HTTPException(502, f"Node 宿主目标发现失败: {error}") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


async def _watch_policy(websocket: WebSocket, auth: GatewayAuthContext) -> None:
    store = getattr(websocket.app.state, "federation_policy_store", None)
    if not isinstance(store, FederationPolicyStore):
        raise TypeError("Gateway federation policy 尚未初始化")
    while True:
        changed = store.changed
        require_federation_capability(websocket, auth, "extension_host_manage")
        await changed.wait()


async def _watch_user(websocket: WebSocket, access: UserAccessContext) -> None:
    # 心跳仍由原用户访问入口续租；宿主通道不替闲置客户端续租。
    service = _users(websocket)
    while True:
        expires_at = service.expires_at(access)
        if expires_at is None:
            raise PermissionError("Node 宿主客户端租约已删除")
        remaining = (datetime.fromisoformat(expires_at) - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise PermissionError("Node 宿主客户端租约已过期")
        try:
            async with asyncio.timeout(remaining):
                await access.invalidated.wait()
        except TimeoutError:
            # 仅在已知到期时回读原租约；正常每帧不增加数据库请求。
            continue
        raise PermissionError("Node 宿主客户端租约已撤销")


async def _watch_route(lease: WorkspaceRouteLease) -> None:
    await lease.invalidated.wait()
    raise LookupError("Node 宿主工作区路由已换代")


async def _relay(websocket: WebSocket, upstream: ClientConnection, *, outbound: bool,
                 auth: GatewayAuthContext, access: UserAccessContext | None,
                 leases: list[WorkspaceRouteLease]) -> None:
    while True:
        if outbound:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
        else:
            data = await upstream.recv()
        _check(websocket, auth, access, leases)
        if not isinstance(data, bytes):
            raise TypeError("宿主握手后只接受原协议二进制帧")
        if outbound:
            await upstream.send(data)
        else:
            await websocket.send_bytes(data)


async def _upstream(websocket: WebSocket, *, url: str, headers: dict[str, str], payload: ExtensionHostOpenRequest,
                    auth: GatewayAuthContext, access: UserAccessContext | None,
                    leases: list[WorkspaceRouteLease], expected_client_id: str) -> None:
    async with connect(url, additional_headers=headers, compression=None,
                       open_timeout=10, close_timeout=10) as upstream:
        _check(websocket, auth, access, leases)
        await upstream.send(payload.model_dump_json())
        async with asyncio.timeout(15):
            raw = await upstream.recv()
        if not isinstance(raw, str):
            raise TypeError("Node 宿主缺少通道身份握手")
        binding = ExtensionHostConnectionDTO.model_validate_json(raw)
        if (binding.model_dump(include=set(ExtensionHostOpenRequest.model_fields)) != payload.model_dump()
            or binding.request_id != get_request_id(websocket) or binding.client_id != expected_client_id):
            raise ValueError("Node 宿主返回了不同目标、视图或请求身份")
        _check(websocket, auth, access, leases)
        await websocket.send_text(binding.model_dump_json())
        await run_channel(
            _relay(websocket, upstream, outbound=True, auth=auth, access=access, leases=leases),
            _relay(websocket, upstream, outbound=False, auth=auth, access=access, leases=leases))


@router.websocket("/api/gateway/extension-hosts/channel")
async def extension_host_channel(websocket: WebSocket) -> None:
    request_id = get_request_id(websocket)
    try:
        auth, access, client_id = _access(websocket)
    except HTTPException as error:
        await websocket.send_denial_response(JSONResponse(status_code=error.status_code,
            content={"detail": error.detail, "request_id": request_id}, headers={"X-Request-ID": request_id}))
        return
    await websocket.accept()
    try:
        async with asyncio.timeout(10):
            payload = ExtensionHostOpenRequest.model_validate_json(await websocket.receive_text())
        with _target_route(websocket, gateway_id=payload.gateway_id, workspace_ids=payload.workspace_ids,
                           auth=auth, streaming=True) as route:
            leases = route.leases
            headers = {**route.headers, CLIENT_HEADER: client_id}
            if route.remote:
                expected_client_id = f"peer:{websocket.app.state.registry.gateway_id}:{client_id}"
                path = "api/gateway/extension-hosts/channel"
            else:
                expected_client_id = client_id
                path = "api/internal/extension-hosts/channel"
            url = _websocket_target(route.base_url, path)
            _check(websocket, auth, access, leases)
            # 所有监视与启动在同一作用域；连接等待期间撤权也会取消原上游。
            operations = [
                _upstream(websocket, url=url, headers=headers, payload=payload, auth=auth, access=access,
                          leases=leases, expected_client_id=expected_client_id),
                _watch_policy(websocket, auth), *(_watch_route(lease) for lease in leases),
            ]
            if access is not None:
                operations.append(_watch_user(websocket, access))
            await run_channel(*operations)
        await close_channel(websocket)
    except WebSocketDisconnect:
        return
    except ConnectionClosed as error:
        await close_channel(websocket, code=1000 if error.rcvd is not None and error.rcvd.code == 1000 else 1011,
                              reason="extension_host_upstream_closed")
    except Exception:
        logger.exception("Gateway Node 宿主通道失败: request_id=%s", request_id)
        await close_channel(websocket, code=1011, reason="extension_host_channel_failed")
