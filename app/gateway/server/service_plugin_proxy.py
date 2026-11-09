"""具名服务 HTTP／流路由；只选择原实例，不启动代码或重放请求。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import HTTPConnection
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocketState
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.api.runtime_control import verify_local_control_request
from app.core.async_channel import close_channel, run_channel
from app.core.trace_middleware import get_request_id
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.federation.policy import (
    Capability,
    FederationPolicyStore,
    require_federation_capability,
)
from app.gateway.proxy_upstream import (
    GATEWAY_PROXY_DROPPED_HEADERS,
    TARGET_GATEWAY_ID_HEADER,
    UpstreamResponseBody,
    build_upstream_url,
    filter_hop_by_hop_headers,
    parse_target_gateway_id,
    relay_websocket_client_to_upstream,
    relay_websocket_upstream_to_client,
    send_upstream_request,
)
from app.gateway.server.management_routes import (
    check_management_route,
    management_gateway_route,
)
from app.schemas.canonical_params import CanonicalWorkspaceId
from app.schemas.gateway.service_plugin import PluginBinding

router = APIRouter()
logger = logging.getLogger(__name__)
BINDING_PATH = "/api/gateway/service-plugins/{package_id}/{plugin_id}/services/{service_id}/{path:path}"
SERVICE_PATH = "/api/gateway/service-plugins/{package_id}/{plugin_id}/instances/{instance_id}/services/{service_id}/{path:path}"

BINDING_DEBUG_RUNTIME_PATH = "/api/gateway/service-plugins/{package_id}/{plugin_id}/debug-runtime"
INSTANCE_DEBUG_RUNTIME_PATH = "/api/gateway/service-plugins/{package_id}/{plugin_id}/instances/{instance_id}/debug-runtime"

@dataclass(frozen=True, slots=True)
class _ServiceRoute:
    url: httpx.URL
    headers: dict[str, str]
    stopped: asyncio.Event
    instance_id: str
    gateway_id: str
    remote: bool = False


@contextmanager
def _route(connection: HTTPConnection, auth: GatewayAuthContext, binding: PluginBinding, *,
           instance_id: str | None, service_id: str, path: str, websocket: bool = False) -> Iterator[_ServiceRoute]:
    registry = connection.app.state.registry
    header_gateway = parse_target_gateway_id(connection.headers.get(TARGET_GATEWAY_ID_HEADER))
    query_gateway = parse_target_gateway_id(connection.query_params.get("target_gateway_id"))
    if header_gateway and query_gateway and header_gateway != query_gateway:
        raise ValueError("插件服务目标 Gateway 头部与查询冲突")
    gateway_id = header_gateway or query_gateway or registry.gateway_id
    with management_gateway_route(connection, gateway_id=gateway_id,
        gateway_connection_id=connection.headers.get("X-BoxTeam-Gateway-Connection-Id"), auth=auth) as remote:
        query = [(key, value) for key, value in connection.query_params.multi_items()
                 if key not in {"token", "target_gateway_id"}]
        headers = {key: value for key, value in connection.headers.items()
                   if key.lower() in {"content-type", "content-encoding", "accept", "last-event-id", "mcp-session-id", "mcp-protocol-version", "x-boxteam-plugin-instance-id"}}
        if remote is not None:
            headers.update(remote.headers)
            url = build_upstream_url(remote.base_url, (), connection.url.path.lstrip("/"))
            retired = asyncio.Event()
            def check_remote() -> None:
                try:
                    check_management_route(connection, remote)
                except LookupError:
                    retired.set()
            registry.add_commit_observer(check_remote)
            try:
                check_remote()
                yield _ServiceRoute(url.copy_merge_params(tuple(query)), headers, retired, instance_id or "", gateway_id, True)
            finally:
                registry.remove_commit_observer(check_remote)
            return
        # 工作区插件与 Backend 路由换代分离；Backend 单独重启不能退役原服务连接。
        if binding.workspace_id is not None:
            target = registry.resolve((gateway_id, binding.workspace_id))
            if target.connection_kind != "local":
                raise LookupError("插件服务不属于原生工作区")
        owner = connection.app.state.service_plugins
        header_instance = connection.headers.get("X-BoxTeam-Plugin-Instance-Id")
        if instance_id is not None and header_instance is not None and instance_id != header_instance:
            raise ValueError("插件实例路径与请求头身份冲突")
        selected_instance = instance_id or header_instance
        if selected_instance is None:
            slot = owner.slots.get(owner.key(binding))
            if slot is None or slot.receipt is None:
                raise LookupError("插件绑定尚无可用实例")
            selected_instance = slot.receipt.instance_id
        base_url, token, stopped = owner.service(binding, service_id=service_id,
            instance_id=selected_instance, websocket=websocket)
        headers.update({"X-Local-Token": token, "X-Request-ID": get_request_id(connection),
                        "X-BoxTeam-Gateway-Id": gateway_id})
        if binding.workspace_id is not None:
            headers["X-BoxTeam-Workspace-Id"] = binding.workspace_id
        if binding.package_id == "boxteam.developer-tools" and binding.plugin_id == "terminal" and not websocket:
            # PTY 管理只接受原生 Backend owner 通道；浏览器只持一次性 WS grant。
            if auth.kind != "local" or not isinstance(connection, Request):
                raise PermissionError("Terminal 管理必须经过原工作区资源 owner")
            verify_local_control_request(connection)
            if connection.headers.get("X-BoxTeam-Resource-Owner") != "workspace-terminal":
                raise PermissionError("Terminal 管理缺少工作区资源 owner 身份")
            headers["X-BoxTeam-Resource-Owner"] = "workspace-terminal"
        if binding.package_id == "boxteam.developer-tools" and binding.plugin_id == "browser" and not websocket:
            artifact_read = isinstance(connection, Request) and connection.method == "GET" and re.fullmatch(
                r"api/browsers/[^/]+/(?:downloads|screenshots)/[^/]+", path,
            ) is not None
            if not artifact_read:
                if auth.kind != "local" or not isinstance(connection, Request):
                    raise PermissionError("Browser 管理必须经过原工作区资源 owner")
                verify_local_control_request(connection)
                if connection.headers.get("X-BoxTeam-Resource-Owner") != "workspace-browser":
                    raise PermissionError("Browser 管理缺少工作区资源 owner 身份")
                headers["X-BoxTeam-Resource-Owner"] = "workspace-browser"
                actor = connection.headers.get("X-BoxTeam-Actor")
                if actor is not None:
                    if actor not in {"agent", "user:http"}:
                        raise PermissionError("Browser 操作 actor 无效")
                    headers["X-BoxTeam-Actor"] = actor
        query = [(key, value) for key, value in query if key not in {"package_version", "workspace_id"}]
        yield _ServiceRoute(build_upstream_url(base_url, (), path).copy_merge_params(tuple(query)), headers, stopped, selected_instance, gateway_id)


async def _watch_access(connection: HTTPConnection, auth: GatewayAuthContext, stopped: asyncio.Event,
                        capabilities: tuple[Capability, ...] = ("send", "read")) -> None:
    """每次策略发布核准入；帧转发只传字节，不新增逐帧网络检查。"""
    async def retired() -> None:
        await stopped.wait()

    async def policy() -> None:
        store: FederationPolicyStore = connection.app.state.federation_policy_store
        while True:
            changed = store.changed
            for capability in capabilities:
                require_federation_capability(connection, auth, capability)
            await changed.wait()

    if auth.kind == "local":
        await retired()
    else:
        await run_channel(retired(), policy())
    raise LookupError("插件原实例或 Gateway 路由已退役")


class _ServiceResponse(StreamingResponse):
    """整个 HTTP 响应随原实例／授权退役，空闲 SSE 也能立即结束。"""

    def __init__(self, body: UpstreamResponseBody, *, request: Request, auth: GatewayAuthContext,
                 route: _ServiceRoute, response: httpx.Response,
                 capabilities: tuple[Capability, ...] = ("send", "read")) -> None:
        super().__init__(body, status_code=response.status_code,
            headers=filter_hop_by_hop_headers((key, value) for key, value in response.headers.items()
                if key.lower() not in GATEWAY_PROXY_DROPPED_HEADERS | {
                    "set-cookie", "content-length", "content-encoding"}))
        if route.remote and (response.headers.get("X-BoxTeam-Plugin-Instance-Id") != route.instance_id
            or response.headers.get("X-BoxTeam-Plugin-Gateway-Id") != route.gateway_id):
            raise ValueError("远端插件响应不属于原实例")
        if route.instance_id:
            self.headers["X-BoxTeam-Plugin-Instance-Id"] = route.instance_id
            self.headers["X-BoxTeam-Plugin-Gateway-Id"] = route.gateway_id
        self.request, self.auth, self.route, self.body = request, auth, route, body
        self.capabilities = capabilities

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started, completed = False, False
        revoked: HTTPException | None = None

        async def forward(message: Message) -> None:
            nonlocal started, completed
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
            await send(message)

        async def watch() -> None:
            nonlocal revoked
            try:
                await _watch_access(self.request, self.auth, self.route.stopped, self.capabilities)
            except LookupError as error:
                revoked = HTTPException(409, str(error))
            except HTTPException as error:
                revoked = error

        try:
            await run_channel(super().__call__(scope, receive, forward), watch())
            if revoked is not None:
                if not started:
                    await JSONResponse(status_code=revoked.status_code,
                        content={"detail": revoked.detail, "request_id": get_request_id(self.request)})(scope, receive, send)
                elif not completed:
                    # 已发送响应头后，撤权／退役只结束原流，不能再发送另一个 HTTP 错误。
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await self.body.aclose()


@router.api_route(BINDING_PATH, methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    response_model=None, include_in_schema=False)
@router.api_route(SERVICE_PATH, methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    response_model=None, include_in_schema=False)
async def service_request(package_id: str, plugin_id: str, service_id: str,
    path: str, package_version: str, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    instance_id: str | None = None, workspace_id: CanonicalWorkspaceId | None = None,
) -> StreamingResponse:
    artifact_read = (package_id == "boxteam.developer-tools" and plugin_id == "browser"
        and request.method == "GET" and re.fullmatch(
            r"api/browsers/[^/]+/(?:downloads|screenshots)/[^/]+", path,
        ) is not None)
    capabilities: tuple[Capability, ...] = ("read",) if artifact_read else ("send", "read")
    for capability in capabilities:
        require_federation_capability(request, auth, capability)
    binding = PluginBinding(package_id=package_id, plugin_id=plugin_id,
        package_version=package_version, enabled=True, workspace_id=workspace_id)
    # 路由引用一直留到响应体完成；不能在返回 StreamingResponse 时提前归还。
    routes = _route(request, auth, binding, instance_id=instance_id, service_id=service_id, path=path)
    try:
        route = routes.__enter__()
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    body: UpstreamResponseBody | None = None
    handed_off = False
    def release_route() -> None:
        routes.__exit__(None, None, None)

    try:
        if route.stopped.is_set():
            raise HTTPException(409, "插件原路由已退役")
        client = request.app.state.service_plugins.client
        upstream = client.build_request(request.method, route.url, content=request.stream(),
            headers=route.headers, timeout=None)
        response: httpx.Response | None = None

        async def open_response() -> None:
            nonlocal response, body
            response = await send_upstream_request(client, upstream)
            body = UpstreamResponseBody(response, release_route)

        # 响应头等待也属于原连接；撤权或退役须取消这次发送，不等待网络超时。
        await run_channel(open_response(), _watch_access(request, auth, route.stopped, capabilities))
        assert response is not None and body is not None
        for capability in capabilities:
            require_federation_capability(request, auth, capability)
        result = _ServiceResponse(body, request=request, auth=auth, route=route, response=response,
            capabilities=capabilities)
        handed_off = True
        return result
    except httpx.RequestError as error:
        logger.exception("插件上游请求失败: request_id=%s instance_id=%s service_id=%s",
            get_request_id(request), instance_id, service_id)
        raise HTTPException(502, f"插件上游连接失败: instance_id={instance_id}, service_id={service_id}, "
            f"error_type={type(error).__name__}；请查看 Gateway 服务日志") from error
    except TimeoutError as error:
        raise HTTPException(504, f"插件上游响应头超时: instance_id={instance_id}, service_id={service_id}") from error
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    finally:
        if not handed_off:
            if body is not None:
                await body.aclose()
            else:
                routes.__exit__(None, None, None)


@router.websocket(SERVICE_PATH)
async def service_websocket(websocket: WebSocket, package_id: str, plugin_id: str, instance_id: str,
    service_id: str, path: str, package_version: str, workspace_id: CanonicalWorkspaceId | None = None,
) -> None:
    try:
        auth = verify_gateway_access(
            x_local_token=websocket.headers.get("x-local-token") or websocket.query_params.get("token"),
            x_boxteam_federation_token=websocket.headers.get("x-boxteam-federation-token"))
        require_federation_capability(websocket, auth, "send")
        require_federation_capability(websocket, auth, "read")
        binding = PluginBinding(package_id=package_id, plugin_id=plugin_id, package_version=package_version,
            enabled=True, workspace_id=workspace_id)
        with _route(websocket, auth, binding, instance_id=instance_id, service_id=service_id,
            path=path, websocket=True) as route:
            if route.stopped.is_set():
                raise LookupError("插件原路由已退役")
            url = str(route.url.copy_with(scheme="wss" if route.url.scheme == "https" else "ws"))

            async def relay() -> None:
                async with connect(url, additional_headers=route.headers, compression=None,
                    max_queue=1, write_limit=256 * 1024, open_timeout=10, close_timeout=5) as upstream:
                    await websocket.accept()
                    await run_channel(relay_websocket_client_to_upstream(websocket, upstream),
                        relay_websocket_upstream_to_client(websocket, upstream))

            await run_channel(relay(), _watch_access(websocket, auth, route.stopped))
    except PermissionError as error:
        await websocket.send_denial_response(JSONResponse(status_code=403,
            content={"detail": str(error), "request_id": get_request_id(websocket)}))
    except HTTPException as error:
        if websocket.application_state == WebSocketState.CONNECTING:
            await websocket.send_denial_response(JSONResponse(status_code=error.status_code,
                content={"detail": error.detail, "request_id": get_request_id(websocket)}))
        else:
            await close_channel(websocket, code=1008, reason="service_access_revoked")
    except (LookupError, ValueError) as error:
        logger.info("插件原连接失效: request_id=%s error=%s", get_request_id(websocket), error)
        if websocket.application_state == WebSocketState.CONNECTING:
            await websocket.send_denial_response(JSONResponse(status_code=409,
                content={"detail": str(error), "request_id": get_request_id(websocket)}))
        else:
            await close_channel(websocket, code=1008, reason="service_instance_retired")
    except (ConnectionClosed, WebSocketDisconnect):
        await close_channel(websocket)
    except InvalidStatus as error:
        # 原生目标的准入拒绝仍归原请求，Relay 不将它伪报为内部服务故障。
        await websocket.send_denial_response(Response(bytes(error.response.body), status_code=error.response.status_code,
            headers=filter_hop_by_hop_headers((key, value) for key, value in error.response.headers.raw_items()
                if key.lower() not in GATEWAY_PROXY_DROPPED_HEADERS | {"set-cookie"})))
    except Exception:
        logger.exception("插件 WebSocket 连接失败: request_id=%s instance_id=%s service_id=%s",
            get_request_id(websocket), instance_id, service_id)
        await close_channel(websocket, code=1011, reason="service_upstream_failed")


@router.websocket(BINDING_DEBUG_RUNTIME_PATH)
@router.websocket(INSTANCE_DEBUG_RUNTIME_PATH)
async def debug_runtime_websocket(
    websocket: WebSocket,
    package_id: str,
    plugin_id: str,
    package_version: str,
    instance_id: str | None = None,
    workspace_id: CanonicalWorkspaceId | None = None,
) -> None:
    """Backend 内部的调试运行控制通道；不占用普通 DAP 业务服务路由。"""
    try:
        if websocket.query_params.get("token") is not None:
            raise PermissionError("调试运行控制不接受查询参数凭据")
        header_workspace = websocket.headers.get("x-boxteam-workspace-id")
        if header_workspace and workspace_id and header_workspace != workspace_id:
            raise ValueError("调试运行通道的 Workspace 头与查询参数冲突")
        workspace_id = workspace_id or header_workspace
        if not workspace_id:
            raise ValueError("调试运行通道必须明确指定 workspace_id")
        auth = verify_gateway_access(
            x_local_token=websocket.headers.get("x-local-token"),
            x_boxteam_federation_token=websocket.headers.get("x-boxteam-federation-token"),
        )
        if auth.kind != "local":
            raise PermissionError("调试运行控制只接受本机 Workspace Backend")
        require_federation_capability(websocket, auth, "send")
        require_federation_capability(websocket, auth, "read")
        binding = PluginBinding(
            package_id=package_id, plugin_id=plugin_id, package_version=package_version,
            enabled=True, workspace_id=workspace_id,
        )
        with _route(
            websocket, auth, binding, instance_id=instance_id, service_id="dap",
            path="__debug_runtime__", websocket=True,
        ) as route:
            owner = websocket.app.state.service_plugins
            slot = owner.slots.get(owner.key(binding))
            if slot is None or slot.declaration.runtime is None or not any(
                contribution.service_id == "dap" for contribution in slot.declaration.contributes.debuggers
            ):
                raise LookupError("插件未声明可用的 DAP 调试器服务")
            if route.remote:
                raise PermissionError("调试运行控制必须由原生 Gateway 转发")
            if route.stopped.is_set():
                raise LookupError("插件原路由已退役")
            # _route 精确选择、校验并持有原 DAP service 实例；控制通道映射到该实例的具名内部端点。
            service_suffix = "/services/dap/__debug_runtime__"
            if not route.url.path.endswith(service_suffix):
                raise LookupError("插件 DAP service 路径与调试运行通道不符")
            path = route.url.path[:-len(service_suffix)] + "/debug-runtime"
            url = str(route.url.copy_with(path=path, scheme="wss" if route.url.scheme == "https" else "ws"))
            package = owner.packages.get(binding.package_id, binding.package_version)
            ready = {
                "version": 1, "type": "ready", "gateway_id": route.gateway_id,
                "workspace_id": workspace_id, "package_id": binding.package_id,
                "plugin_id": binding.plugin_id, "package_version": binding.package_version,
                "package_digest": package.digest,
                "extension_contract_version": package.manifest.extension_contract_version,
                "declaration_revision": slot.revision,
                "instance_id": route.instance_id, "service_id": "dap", "contract_version": 1,
                "debuggers": [
                    debugger.model_dump(mode="json")
                    for debugger in slot.declaration.contributes.debuggers
                ],
            }

            async def relay_debug_runtime() -> None:
                async with connect(
                    url, additional_headers=route.headers, compression=None, max_queue=1,
                    write_limit=256 * 1024, open_timeout=10, close_timeout=5,
                ) as upstream:
                    await websocket.accept()
                    await websocket.send_json(ready)
                    await run_channel(
                        relay_websocket_client_to_upstream(websocket, upstream),
                        relay_websocket_upstream_to_client(websocket, upstream),
                    )

            await run_channel(relay_debug_runtime(), _watch_access(websocket, auth, route.stopped))
    except PermissionError as error:
        await websocket.send_denial_response(JSONResponse(status_code=403,
            content={"detail": str(error), "request_id": get_request_id(websocket)}))
    except HTTPException as error:
        if websocket.application_state == WebSocketState.CONNECTING:
            await websocket.send_denial_response(JSONResponse(status_code=error.status_code,
                content={"detail": error.detail, "request_id": get_request_id(websocket)}))
        else:
            await close_channel(websocket, code=1008, reason="service_access_revoked")
    except (LookupError, ValueError) as error:
        logger.info("插件调试运行通道不可用: request_id=%s error=%s", get_request_id(websocket), error)
        if websocket.application_state == WebSocketState.CONNECTING:
            await websocket.send_denial_response(JSONResponse(status_code=409,
                content={"detail": str(error), "request_id": get_request_id(websocket)}))
        else:
            await close_channel(websocket, code=1008, reason="service_instance_retired")
    except (ConnectionClosed, WebSocketDisconnect):
        await close_channel(websocket)
    except InvalidStatus as error:
        await websocket.send_denial_response(Response(bytes(error.response.body), status_code=error.response.status_code,
            headers=filter_hop_by_hop_headers((key, value) for key, value in error.response.headers.raw_items()
                if key.lower() not in GATEWAY_PROXY_DROPPED_HEADERS | {"set-cookie"})))
    except Exception:
        logger.exception("插件调试运行通道连接失败: request_id=%s instance_id=%s",
            get_request_id(websocket), instance_id)
        await close_channel(websocket, code=1011, reason="debug_runtime_upstream_failed")
