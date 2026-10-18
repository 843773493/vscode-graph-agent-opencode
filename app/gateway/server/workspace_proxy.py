from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from functools import partial
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.core.history_loading import HistoryLoadingConfig
from app.core.path_utils import get_gateway_root
from app.core.resource_uri import ResourceUriGrammarError
from app.core.trace_middleware import NAVIGATION_ACTOR_HEADER, get_request_id
from app.gateway.auth import LOCAL_TOKEN, GatewayAuthContext, verify_gateway_access
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessContext,
    UserAccessService,
)
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.errors import FederationError
from app.gateway.federation.grants import ContextGrantTarget
from app.gateway.federation.messages import FEDERATION_DEADLINE_SECONDS
from app.gateway.protocol.proxy import proxy_target_to_proto
from app.gateway.proxy_upstream import (
    GATEWAY_PROXY_DROPPED_HEADERS,
    UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS,
    build_upstream_url,
    filter_hop_by_hop_headers,
    load_proxy_gateway_id,
    run_cleanup_shielded,
    send_upstream_request,
)
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceRouteLease,
    WorkspaceTarget,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.communication import ContextQueryRequest
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextSearchRequest,
)
from app.services.business.session_context_resource import parse_session_context_ref
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)

router = APIRouter()

HISTORY_LOADING_HEADER = "x-boxteam-history-loading"
BACKEND_WORKSPACE_ID_HEADER = "x-boxteam-backend-workspace-id"
# 工作区 API 代理在逐跳头部之外还要剥离会话加载策略与浏览器 Cookie；Gateway 凭据
# 与目标选择头部共用代理的同一份集合。
PROXY_ONLY_DROPPED_HEADERS = GATEWAY_PROXY_DROPPED_HEADERS | {
    HISTORY_LOADING_HEADER,
    BACKEND_WORKSPACE_ID_HEADER,
    "cookie",
}
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
        or path.endswith("/observations/stream")
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


def _navigation_actor(
    auth: GatewayAuthContext | None,
    user_access: UserAccessContext | None,
) -> str | None:
    """从本跳已验证身份构造导航 operation 的幂等 actor。"""
    if auth is None:
        return None
    if auth.kind == "federation":
        peer_gateway_id = auth.peer_gateway_id
        if not isinstance(peer_gateway_id, str) or not peer_gateway_id:
            raise RuntimeError("Gateway federation 凭据缺少已验证的 peer_gateway_id")
        return f"federation:{peer_gateway_id}"
    if auth.kind != "local":
        raise RuntimeError(f"Gateway 认证类型不受支持: {auth.kind!r}")
    if user_access is None:
        raise RuntimeError("本地 Gateway API 代理缺少已验证的 UserAccess")
    if user_access.kind == "user":
        if not isinstance(user_access.user_id, str) or not user_access.user_id:
            raise RuntimeError("Gateway 已验证的 UserAccess 缺少 user_id")
        return f"user:{user_access.user_id}"
    if user_access.kind == "guest":
        return "guest"
    raise RuntimeError(f"Gateway UserAccess 类型不受支持: {user_access.kind!r}")


def _proxy_headers(
    request: Request,
    target: WorkspaceTarget | None = None,
    *,
    include_credentials: bool = True,
    navigation_actor: str | None = None,
) -> dict[str, str]:
    headers = filter_hop_by_hop_headers(
        (key, value)
        for key, value in request.headers.items()
        if key.lower() not in PROXY_ONLY_DROPPED_HEADERS
    )
    headers["X-Request-ID"] = get_request_id(request)
    # 按请求注入 Gateway 自身稳定身份；客户端同名头部已在上面的剥离集合里剔除，
    # 故这里写入的永远是本机 Gateway 的权威 gateway_id。
    headers["X-BoxTeam-Gateway-Id"] = load_proxy_gateway_id()
    if navigation_actor is not None:
        headers[NAVIGATION_ACTOR_HEADER] = navigation_actor
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
    elif target is not None:
        backend_workspace_id = target.backend_workspace_id
        if backend_workspace_id is None:
            raise RuntimeError(
                "本地 Gateway target 缺少 backend UUID，拒绝代理: "
                f"workspace_id={target.workspace_id}"
            )
        headers["X-BoxTeam-Workspace-Id"] = backend_workspace_id
        if include_credentials:
            headers["X-Local-Token"] = LOCAL_TOKEN
    elif include_credentials:
        headers["X-Local-Token"] = LOCAL_TOKEN
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return filter_hop_by_hop_headers(response.headers.items())


def _set_authoritative_header(
    headers: dict[str, str],
    name: str,
    value: str,
) -> None:
    """按大小写不敏感地写入 Gateway 自己的权威头部值。

    httpx 把上游响应头名统一小写，而这里写入的是规范拼写。若上游带了同名头部
    （嵌套 Gateway 也会带自己的 X-BoxTeam-Route-Revision），直接赋值会在同一个
    dict 里留下两个仅大小写不同的键；Starlette 原样序列化成两条同名头部，浏览器
    的大小写不敏感 get() 只取第一条，读到的是上游值而非本机权威值。先剔除全部
    同名条目再写入。
    """
    for key in [key for key in headers if key.lower() == name.lower()]:
        del headers[key]
    headers[name] = value


async def _stream_proxy_response(
    response: httpx.Response,
    route_lease: WorkspaceRouteLease,
    user_access: UserAccessContext | None,
    on_close: Callable[[], None] | None = None,
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
        await run_cleanup_shielded(
            partial(_close_proxy_response, response, pending_tasks, on_close)
        )


async def _close_proxy_response(
    response: httpx.Response,
    pending_tasks: list[asyncio.Task[object]],
    on_close: Callable[[], None] | None,
) -> None:
    """回收上游响应与本路由的代理引用；在取消下也必须完整跑完。"""

    await asyncio.gather(*pending_tasks, return_exceptions=True)
    try:
        await response.aclose()
    finally:
        if on_close is not None:
            on_close()


async def _stream_proxy_body(
    response: httpx.Response,
    on_close: Callable[[], None] | None = None,
) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await run_cleanup_shielded(
            partial(_close_proxy_response, response, [], on_close)
        )


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
    target_workspace_id: str | None = None,
    request_body: bytes | None = None,
) -> Response:
    registry = _registry(request)
    workspace_id = target_workspace_id or request.headers.get("X-BoxTeam-Workspace-Id")
    try:
        target = registry.resolve(workspace_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    requested_backend_workspace_id = request.headers.get(
        BACKEND_WORKSPACE_ID_HEADER
    )
    if requested_backend_workspace_id is not None and (
        target.connection_kind != "local"
        or requested_backend_workspace_id != target.backend_workspace_id
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Gateway target 与 backend_workspace_id 不一致: "
                f"workspace_id={target.workspace_id}, "
                f"backend_workspace_id={requested_backend_workspace_id!r}"
            ),
        )
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
    request_content = request_body if request_body is not None else (
        request.stream()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}
        else None
    )
    # 认证主体与导航幂等 scope 分离；只有已解析的两条 operation 路由消费 actor。
    navigation_actor = (
        _navigation_actor(auth, user_access)
        if (request.method, path) in {
            ("POST", "session-catalog/operations:enqueue"),
            ("GET", "session-catalog/operations"),
        }
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
    # 路由引用从获取到释放必须构成配对作用域：header 构造、运行时等待、重试解析
    # 中任何一步抛错，以及客户端在等待响应头时断连触发的取消，都必须归还这一份
    # 引用。整个处理流程包在一个 try 里，由 finally 兜底释放；同步闭包无需 shield
    # （finally 内的同步调用不可被取消打断），流式响应则把租约移交给响应体生成器
    # 的单释放点，移交前先重绑定标志，避免在这里被二次释放。
    response: httpx.Response | None = None
    target_url = ""
    reference_streaming = False
    route_reference_released = False
    route_reference_handed_off = False
    route_workspace_id = target.workspace_id
    route_lease = registry.acquire_route_reference(
        route_workspace_id,
        streaming=False,
    )

    def release_route_reference() -> None:
        nonlocal route_reference_released
        if route_reference_released:
            return
        route_reference_released = True
        registry.release_route_reference(
            route_workspace_id,
            streaming=reference_streaming,
        )

    try:
        for attempt in range(len(retry_delays) + 1):
            if attempt > 0:
                await asyncio.sleep(retry_delays[attempt - 1])
                target = registry.resolve(route_workspace_id)
                route_workspace_id = target.workspace_id
                release_route_reference()
                route_lease = registry.acquire_route_reference(
                    route_workspace_id,
                    streaming=False,
                )
                reference_streaming = False
                route_reference_released = False
            if (
                target.connection_kind == "remote_gateway"
                and target.remote_gateway_connection_id is not None
            ):
                try:
                    target_url = build_upstream_url(
                        registry.remote_gateway_url(
                            target.remote_gateway_connection_id
                        ),
                        ("api", "v1"),
                        path,
                    )
                except ValueError as error:
                    raise HTTPException(
                        status_code=400,
                        detail=str(error),
                    ) from error
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
                try:
                    target_url = build_upstream_url(
                        backend_url,
                        ("api", "v1"),
                        path,
                    )
                except ValueError as error:
                    raise HTTPException(
                        status_code=400,
                        detail=str(error),
                    ) from error
            # 联邦凭据缺失/过期在这里响亮失败；此处抛错也必须归还上面持有的引用。
            headers = _proxy_headers(
                request,
                target,
                include_credentials=include_credentials,
                navigation_actor=navigation_actor,
            )
            if request_body is not None:
                # typed 请求已由路由 owner 重写，长度必须属于重写后的 UTF-8 字节。
                _set_authoritative_header(headers, "Content-Length", str(len(request_body)))
            forwarded = client.build_request(
                request.method,
                target_url,
                params=request.query_params.multi_items(),
                content=request_content,
                headers=headers,
            )
            try:
                response = await send_upstream_request(client, forwarded)
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
            except TimeoutError as error:
                if retry_upstream and attempt < len(retry_delays):
                    continue
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "工作区后端在有限等待时间内未返回响应头: "
                        f"{target_url}, "
                        f"timeout_seconds={UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS:g}"
                    ),
                ) from error
        if response is None:
            raise HTTPException(
                status_code=502,
                detail=f"工作区 availability 请求未获得响应: {target_url}",
            )
        media_type = response.headers.get("content-type")
        if media_type and "text/event-stream" in media_type:
            headers = _response_headers(response)
            # 路由 revision 是本机权威信息，上游的同名头部一律被这里覆盖。
            _set_authoritative_header(
                headers,
                "X-BoxTeam-Route-Revision",
                route_lease.token,
            )
            release_route_reference()
            route_lease = registry.acquire_route_reference(
                route_workspace_id,
                streaming=True,
            )
            reference_streaming = True
            route_reference_released = False
            route_reference_handed_off = True
            return StreamingResponse(
                _stream_proxy_response(
                    response,
                    route_lease,
                    user_access,
                    release_route_reference,
                ),
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
        # 非 SSE 的流式响应体仍持有这份引用，直到 body 真正结束才由 on_close 归还。
        route_reference_handed_off = True
        return StreamingResponse(
            _stream_proxy_body(response, release_route_reference),
            status_code=response.status_code,
            headers=headers,
            media_type=media_type,
        )
    finally:
        # 客户端断连会在任意 await 点抛出 CancelledError；引用释放是同步闭环，
        # 放在 finally 里即可保证归还，且取消仍会继续向上传播。已移交给响应体
        # 生成器的引用不在这里释放，由生成器的 on_close 单点归还。
        if not route_reference_handed_off:
            release_route_reference()


async def _proxy_context_request(
    request: Request,
    query: SessionContextReadRequest | SessionContextSearchRequest,
    *,
    auth: GatewayAuthContext,
    user_access: UserAccessContext | None,
) -> Response:
    """结构化 URI 由原 registry 选归属路由，目标上下文 owner 决定内容。"""
    search = isinstance(query, SessionContextSearchRequest)
    try:
        resource = parse_session_context_ref(
            resource_identity=query.resource_identity, uri=query.uri, scope=query.scope,
            local_gateway_id=request.app.state.gateway_id,
        )
        query = query.model_copy(update={"uri":resource.uri})
        if resource.gateway_authority is None and resource.scope == "workspace":
            targets = [
                target for target in _registry(request).targets()
                if target.connection_kind == "local" and target.backend_workspace_id == resource.scope_id
            ]
            if not targets:
                raise HTTPException(status_code=404, detail="上下文 workspace 未注册或不存在")
            if len(targets) != 1:
                raise RuntimeError("多个本地 Gateway route 指向同一上下文 workspace")
            return await _proxy_workspace_request(
                "context/search" if search else "context/read", request,
                auth=auth, user_access=user_access, include_credentials=True,
                target_workspace_id=targets[0].workspace_id,
                request_body=query.model_dump_json().encode("utf-8"),
            )
        if auth.kind == "federation":
            raise HTTPException(status_code=400, detail="bounded federation 上下文资源必须通过原 read grant relay")
        service = request.app.state.federation_rpc_service
        target = ContextGrantTarget(
            gateway_id=resource.gateway_authority or service.gateway_id,
            resource_identity=str(resource.resource_identity), uri=resource.uri, scope=resource.scope,
        )
        envelope = ContextQueryRequest(
            source_gateway_id=service.gateway_id, resource_target=target,
            read=None if search else query, search=query if search else None,
        )
        result = await service.relay_operation(
            target=target, operation="read", body=envelope.model_dump(mode="json"),
            deadline_at=time.time() + FEDERATION_DEADLINE_SECONDS,
        )
        while isinstance(result.get("result"), dict) and "relayed_by" in result:
            result = result["result"]
    except FederationError as error:
        raise HTTPException(status_code=409, detail=error.to_wire()) from error
    except (ResourceUriGrammarError, ResourceUriResolveError) as error:
        raise HTTPException(
            status_code=400, detail={"code":error.reason_code,"message":str(error)},
        ) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    request_id = get_request_id(request)
    return Response(
        content=APIResponse(data=result, request_id=request_id).model_dump_json(),
        media_type="application/json", headers={"X-Request-ID":request_id},
    )


@router.api_route("/api/v1/context/read", methods=["POST"])
async def proxy_context_read(
    payload: SessionContextReadRequest,
    request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    user_access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> Response:
    return await _proxy_context_request(request, payload, auth=auth, user_access=user_access)


@router.api_route("/api/v1/context/search", methods=["POST"])
async def proxy_context_search(
    payload: SessionContextSearchRequest,
    request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    user_access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> Response:
    return await _proxy_context_request(request, payload, auth=auth, user_access=user_access)


@router.post(
    "/api/v1/workspaces/{backend_workspace_id}/resources/navigation",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ResourceNavigationRequest"},
                },
            },
        },
        "responses": {
            "200": {
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/APIResponse_ResourceNavigationResultDTO_"},
                    },
                },
            },
        },
    },
)
async def proxy_resource_navigation(
    backend_workspace_id: str,
    request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    user_access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> Response:
    # canonical 路径已经固定 Workspace 身份，不能再由当前激活 target 改选上游。
    targets = [
        target for target in _registry(request).targets()
        if target.connection_kind == "local" and target.backend_workspace_id == backend_workspace_id
    ]
    if not targets:
        raise HTTPException(status_code=404, detail="导航 workspace 未注册或不存在")
    if len(targets) != 1:
        raise HTTPException(status_code=409, detail="多个本地 Gateway target 指向同一导航 workspace")
    return await _proxy_workspace_request(
        f"workspaces/{backend_workspace_id}/resources/navigation", request,
        auth=auth, user_access=user_access, include_credentials=True,
        target_workspace_id=targets[0].workspace_id,
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
