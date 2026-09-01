from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.core.history_loading import HistoryLoadingConfig
from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import get_request_id
from app.gateway.auth import LOCAL_TOKEN, GatewayAuthContext, verify_gateway_access
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessContext,
    UserAccessService,
)
from app.gateway.credentials import FederationCredentialStore
from app.gateway.protocol.proxy import proxy_target_to_proto
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceRouteLease,
    WorkspaceTarget,
)

router = APIRouter()

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
HISTORY_LOADING_HEADER = "x-boxteam-history-loading"
MESSAGE_STREAM_AVAILABILITY_RETRY_DELAYS_SECONDS = (0.05,)
MESSAGE_STREAM_RETRY_DELAYS_SECONDS = (0.05, 0.25, 0.75)
WORKSPACE_RUNTIME_READY_WAIT_SECONDS = 120.0


def _registry(request: Request) -> GatewayWorkspaceRegistry:
    registry = getattr(request.app.state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway registry 尚未初始化")
    return registry


def _http_client(request: Request, *, streaming: bool = False) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if streaming:
        client = getattr(request.app.state, "streaming_http_client", None)
    if not isinstance(client, httpx.AsyncClient):
        client_name = "streaming_http_client" if streaming else "http_client"
        raise RuntimeError(f"Gateway {client_name} 尚未初始化")
    return client


def _is_streaming_workspace_path(path: str) -> bool:
    """判断工作区 API 是否会返回长期占用连接的 SSE。"""
    return (
        path.endswith("/traces/stream")
        or path.endswith("/events/stream")
        or path.endswith("/files/events")
        or path.endswith("/message-stream")
    )


def _is_retryable_message_stream_availability(
    method: str,
    path: str,
) -> bool:
    return method in {"GET", "HEAD"} and path.endswith(
        "/message-streams/availability"
    )


def _is_retryable_message_stream(
    method: str,
    path: str,
) -> bool:
    """在 Turn 的流资源刚创建时，隐藏一次短暂的上游 404。"""
    return method == "GET" and path.endswith("/message-stream")


def _user_access_service(request: Request) -> UserAccessService:
    service = getattr(request.app.state, "user_access_service", None)
    if not isinstance(service, UserAccessService):
        raise RuntimeError("Gateway 用户访问服务尚未初始化")
    return service


def verify_user_access_for_proxy(
    request: Request,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
) -> UserAccessContext | None:
    # 联邦请求已经由上游 Gateway 持有并校验用户访问租约，不能把浏览器 Cookie
    # 传播到下游 Gateway 或 Workspace Backend。
    if auth.kind == "federation":
        return None
    context = _user_access_service(request).resolve_cookie(
        request.cookies.get(USER_ACCESS_COOKIE_NAME)
    )
    if context is None:
        raise HTTPException(status_code=401, detail="user_session_required")
    return context


def _proxy_headers(
    request: Request,
    target: WorkspaceTarget | None = None,
    *,
    include_credentials: bool = True,
) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower()
        not in {
            "host",
            "x-local-token",
            "x-boxteam-federation-token",
            "x-boxteam-workspace-id",
            HISTORY_LOADING_HEADER,
            "cookie",
        }
    }
    headers["X-Request-ID"] = get_request_id(request)
    application = request.scope.get("app")
    gateway_config = getattr(getattr(application, "state", None), "gateway_config", None)
    history_loading = getattr(gateway_config, "history_loading", None)
    if isinstance(history_loading, HistoryLoadingConfig):
        # 每一跳都覆盖入站策略，确保会话所属 Gateway 是最终权威来源。
        headers["X-BoxTeam-History-Loading"] = history_loading.as_header_value()
    if target is not None and target.connection_kind == "remote_gateway":
        connection_id = target.remote_gateway_connection_id
        remote_workspace_id = target.remote_workspace_id
        if connection_id is None or remote_workspace_id is None:
            raise RuntimeError("远程投影工作区缺少 Gateway 连接信息")
        headers["X-BoxTeam-Workspace-Id"] = remote_workspace_id
        if include_credentials:
            credential = FederationCredentialStore(
                storage_path=get_gateway_root() / "credentials" / "federation.json"
            ).get(connection_id)
            headers["X-BoxTeam-Federation-Token"] = credential.token
    elif include_credentials:
        headers["X-Local-Token"] = LOCAL_TOKEN
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


async def _stream_proxy_response(
    response: httpx.Response,
    route_lease: WorkspaceRouteLease,
    user_access: UserAccessContext | None,
) -> AsyncIterator[bytes]:
    iterator = response.aiter_bytes()
    next_chunk = asyncio.create_task(anext(iterator))
    route_changed = asyncio.create_task(route_lease.invalidated.wait())
    user_session_changed = (
        asyncio.create_task(user_access.invalidated.wait())
        if user_access is not None
        else None
    )
    try:
        while True:
            wait_tasks = {next_chunk, route_changed}
            if user_session_changed is not None:
                wait_tasks.add(user_session_changed)
            completed, _ = await asyncio.wait(
                wait_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if user_session_changed is not None and user_session_changed in completed:
                yield b": user-session-invalidated\n\n"
                return
            if route_changed in completed:
                yield b": gateway-route-invalidated\n\n"
                return
            try:
                chunk = next_chunk.result()
            except StopAsyncIteration:
                return
            yield chunk
            next_chunk = asyncio.create_task(anext(iterator))
    finally:
        for task in (next_chunk, route_changed, user_session_changed):
            if task is None:
                continue
            if not task.done():
                task.cancel()
        pending_tasks = [next_chunk, route_changed]
        if user_session_changed is not None:
            pending_tasks.append(user_session_changed)
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        await response.aclose()


async def _stream_proxy_body(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await response.aclose()


async def _wait_for_workspace_runtime(
    request: Request,
    target: WorkspaceTarget,
) -> None:
    """等待启动恢复任务的有界结果，再解析本地工作区路由。"""
    application = request.scope.get("app")
    if application is None:
        return
    restore_tasks = getattr(
        getattr(application, "state", None),
        "managed_runtime_restore_tasks",
        {},
    )
    if not isinstance(restore_tasks, dict):
        return
    restore_task = restore_tasks.get(target.workspace_id)
    if not isinstance(restore_task, asyncio.Task) or restore_task.done():
        if isinstance(restore_task, asyncio.Task) and restore_task.done():
            error = restore_task.exception()
            if error is not None:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "工作区后端启动失败: "
                        f"workspace_id={target.workspace_id}: {error}"
                    ),
                ) from error
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(restore_task),
            timeout=WORKSPACE_RUNTIME_READY_WAIT_SECONDS,
        )
    except TimeoutError as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "工作区后端在有限等待时间内未就绪，请稍后重试: "
                f"workspace_id={target.workspace_id}, "
                f"timeout_seconds={WORKSPACE_RUNTIME_READY_WAIT_SECONDS:g}"
            ),
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "工作区后端启动失败: "
                f"workspace_id={target.workspace_id}: {error}"
            ),
        ) from error


async def _proxy_workspace_request(
    path: str,
    request: Request,
    *,
    auth: GatewayAuthContext | None,
    user_access: UserAccessContext | None,
    include_credentials: bool,
) -> Response:
    registry = _registry(request)
    workspace_id = request.headers.get("X-BoxTeam-Workspace-Id")
    try:
        target = registry.resolve(workspace_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    proxy_target_to_proto(
        workspace_id=target.workspace_id,
        service="workspace_api",
        path=f"/{path}",
    )
    if (
        auth is not None
        and auth.kind == "federation"
        and target.connection_kind != "local"
    ):
        raise HTTPException(
            status_code=400,
            detail="bounded federation 禁止通过远程 Gateway 继续代理嵌套工作区",
        )
    client = _http_client(
        request,
        streaming=_is_streaming_workspace_path(path),
    )
    request_content = (
        request.stream()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}
        else None
    )
    retry_availability = _is_retryable_message_stream_availability(
        request.method,
        path,
    )
    retry_message_stream = _is_retryable_message_stream(request.method, path)
    retry_upstream = retry_availability or retry_message_stream
    retry_delays = (
        MESSAGE_STREAM_RETRY_DELAYS_SECONDS
        if retry_message_stream
        else MESSAGE_STREAM_AVAILABILITY_RETRY_DELAYS_SECONDS
        if retry_availability
        else ()
    )
    response: httpx.Response | None = None
    target_url = ""
    route_lease = registry.route_lease(target.workspace_id)
    for attempt in range(len(retry_delays) + 1):
        if attempt > 0:
            await asyncio.sleep(retry_delays[attempt - 1])
            target = registry.resolve(target.workspace_id)
            route_lease = registry.route_lease(target.workspace_id)
        if (
            target.connection_kind == "remote_gateway"
            and target.remote_gateway_connection_id is not None
        ):
            target_url = (
                f"{registry.remote_gateway_url(target.remote_gateway_connection_id)}"
                f"/api/v1/{path}"
            )
        else:
            await _wait_for_workspace_runtime(request, target)
            try:
                backend_url = registry.resolve_service_url(
                    target.workspace_id,
                    "workspace_api",
                )
            except LookupError as error:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "工作区后端正在启动或尚未连接，请稍后重试: "
                        f"workspace_id={target.workspace_id}"
                    ),
                ) from error
            target_url = f"{backend_url.rstrip('/')}/api/v1/{path}"
        forwarded = client.build_request(
            request.method,
            target_url,
            params=request.query_params,
            content=request_content,
            headers=_proxy_headers(
                request,
                target,
                include_credentials=include_credentials,
            ),
        )
        try:
            response = await client.send(forwarded, stream=True)
            if retry_availability:
                # availability 是小型 JSON。提前消费响应体，才能在连接于响应头
                # 之后断开时仍在 Gateway 内重试，而不是把网络错误暴露给浏览器。
                await response.aread()
            if (
                retry_message_stream
                and response.status_code == 404
                and attempt < len(retry_delays)
            ):
                # 首次订阅可能早于 AgentExecutionService 的 store.open()。
                # 关闭并在 Gateway 内重试，避免把可恢复的创建窗口作为浏览器
                # 控制台 404 暴露出来。
                await response.aclose()
                response = None
                continue
            break
        except httpx.RequestError as error:
            if response is not None:
                await response.aclose()
                response = None
            if retry_upstream and attempt < len(retry_delays):
                continue
            raise HTTPException(
                status_code=502,
                detail=f"无法连接工作区后端 {target_url}: {error}",
            ) from error
    if response is None:
        raise HTTPException(
            status_code=502,
            detail=f"工作区 availability 请求未获得响应: {target_url}",
        )
    media_type = response.headers.get("content-type")
    if media_type and "text/event-stream" in media_type:
        headers = _response_headers(response)
        headers["X-BoxTeam-Route-Revision"] = route_lease.token
        return StreamingResponse(
            _stream_proxy_response(response, route_lease, user_access),
            status_code=response.status_code,
            media_type=media_type,
            headers=headers,
        )
    if retry_availability:
        body = response.content
        headers = _response_headers(response)
        await response.aclose()
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=media_type,
        )
    headers = _response_headers(response)
    return StreamingResponse(
        _stream_proxy_body(response),
        status_code=response.status_code,
        headers=headers,
        media_type=media_type,
    )


@router.api_route(
    "/api/v1/context/read",
    methods=["POST"],
)
async def proxy_context_read(request: Request) -> Response:
    """按工作区 ID 转发结构化上下文读取，不要求 Gateway 凭据。"""
    return await _proxy_workspace_request(
        "context/read",
        request,
        auth=None,
        user_access=None,
        include_credentials=False,
    )


@router.api_route(
    "/api/v1/context/search",
    methods=["POST"],
)
async def proxy_context_search(request: Request) -> Response:
    """按工作区 ID 转发结构化上下文搜索，不要求 Gateway 凭据。"""
    return await _proxy_workspace_request(
        "context/search",
        request,
        auth=None,
        user_access=None,
        include_credentials=False,
    )


@router.api_route(
    "/api/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_workspace_api(
    path: str,
    request: Request,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    user_access: UserAccessContext | None = Depends(verify_user_access_for_proxy),
) -> Response:
    return await _proxy_workspace_request(
        path,
        request,
        auth=auth,
        user_access=user_access,
        include_credentials=True,
    )
