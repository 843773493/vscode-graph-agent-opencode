"""独立认证宿主通道；Gateway 只冻结路由、判权和转发原协议。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeVar

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

from app.core.path_utils import get_gateway_root
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
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.policy import (
    Capability,
    FederationPolicyStore,
    federation_capability_allowed,
    require_federation_capability,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceRouteLease
from app.gateway.registry.routes import BackendManagementRoute
from app.gateway.server.extension_management import management_owner
from app.gateway.server.workspace_proxy import (
    _http_client,
    verify_user_access_for_proxy,
)
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationViewRequest,
)
from app.schemas.gateway.extension_host import (
    ExtensionHostConnectionDTO,
    ExtensionHostOpenRequest,
    ExtensionHostTargetDTO,
    ExtensionHostTargetRequest,
    ExtensionTargetCapabilitiesDTO,
    ExtensionTargetDTO,
    ExtensionTargetListDTO,
    ExtensionTargetListRequest,
    ExtensionTargetViewDTO,
)
from app.schemas.gateway.extension_installation import (
    ExtensionInstallationInstallDTO,
    ExtensionInstallationInstallRequest,
    ExtensionInstallationInventoryDTO,
    ExtensionInstallationInventoryRequest,
    ExtensionInstallationTarget,
    ExtensionInstallationUninstallDTO,
    ExtensionInstallationUninstallRequest,
)
from app.schemas.gateway.extension_web import (
    ExtensionWebHostConnectionDTO,
    ExtensionWebHostOpenRequest,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.extension_host.channel import (
    close_channel,
    run_channel,
)
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    ProfileExtensionInventory,
    UninstalledProfile,
)

router = APIRouter()
logger = logging.getLogger(__name__)
CLIENT_HEADER = "X-BoxTeam-Extension-Client-Id"


def _access(websocket: WebSocket) -> tuple[GatewayAuthContext, UserAccessContext | None, str]:
    auth = verify_gateway_access(
        x_local_token=websocket.headers.get("x-local-token") or websocket.query_params.get("token"),
        x_boxteam_federation_token=websocket.headers.get("x-boxteam-federation-token"))
    require_federation_capability(websocket, auth, "extension_host_manage")
    access = None if auth.kind == "federation" else _users(websocket).resolve_cookie(websocket.cookies.get(USER_ACCESS_COOKIE_NAME))
    return auth, access, extension_client_identity(websocket, auth, access)


def extension_client_identity(connection: HTTPConnection, auth: GatewayAuthContext,
                              access: UserAccessContext | None) -> str:
    """WS 和配置 HTTP 共用认证租约身份；公开 body 不接受客户端声明。"""
    if auth.kind == "federation":
        client_id = connection.headers.get(CLIENT_HEADER)
        if not client_id or len(client_id) > 256 or not client_id.isascii():
            raise HTTPException(400, "联邦宿主通道缺少原始客户端绑定")
        # 远端只能在本跳已认证 peer 的命名空间中声明客户端，不能覆盖原生会话。
        return f"peer:{auth.peer_gateway_id}:{client_id}"
    if access is None:
        raise HTTPException(401, "user_session_required")
    # cookie 是访问凭据，只派生不含凭据正文的客户端身份；guest 同样沿既有租约。
    identity = f"{access.kind}:{access.user_id}:{access.access_session_id}:{access.lease_generation}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _users(websocket: HTTPConnection) -> UserAccessService:
    service = getattr(websocket.app.state, "user_access_service", None)
    if not isinstance(service, UserAccessService):
        raise TypeError("Gateway 用户访问服务尚未初始化")
    return service


def _check(websocket: HTTPConnection, auth: GatewayAuthContext, access: UserAccessContext | None,
           leases: list[WorkspaceRouteLease], *, capability: Capability) -> None:
    require_federation_capability(websocket, auth, capability)
    _check_access(access, leases)


def _check_access(access: UserAccessContext | None, leases: list[WorkspaceRouteLease]) -> None:
    if access is not None and access.invalidated.is_set():
        raise PermissionError("Node 宿主客户端租约已失效")
    if any(lease.invalidated.is_set() for lease in leases):
        raise LookupError("Node 宿主工作区路由已失效")


def _extension_capabilities(connection: HTTPConnection, auth: GatewayAuthContext) -> tuple[bool, bool]:
    return (
        federation_capability_allowed(connection, auth, "extension_installation_manage"),
        federation_capability_allowed(connection, auth, "extension_host_manage"),
    )


def _check_inventory_permission(connection: HTTPConnection, auth: GatewayAuthContext) -> tuple[bool, bool]:
    capabilities = _extension_capabilities(connection, auth)
    if not any(capabilities):
        raise PermissionError("目标 Gateway 未授权扩展库存读取")
    return capabilities


def _check_inventory_access(connection: HTTPConnection, auth: GatewayAuthContext,
                            access: UserAccessContext | None, leases: list[WorkspaceRouteLease],
                            expected: tuple[bool, bool]) -> None:
    _check_access(access, leases)
    if _check_inventory_permission(connection, auth) != expected:
        raise PermissionError("扩展库存读取权限在操作期间发生变化，请重新查询")


@dataclass(frozen=True, slots=True)
class _HostRoute:
    base_url: str
    remote: bool
    headers: dict[str, str]
    leases: list[WorkspaceRouteLease]
    connection_id: str | None = None
    connection_identity: tuple[object, object] | None = None
    management_routes: tuple[BackendManagementRoute, ...] = ()


@contextmanager
def _target_route(connection: HTTPConnection, *, gateway_id: str, workspace_ids: list[str],
                  auth: GatewayAuthContext, streaming: bool, gateway_connection_id: str | None = None) -> Iterator[_HostRoute]:
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
            if gateway_connection_id is not None and registry.remote_connection_for_target(targets[0]).connection_id != gateway_connection_id:
                raise ValueError("宿主工作区视图与明确 Gateway 连接不匹配")
            headers.update(registry.target_request_headers(targets[0]))
        else:
            if gateway_connection_id is not None:
                raise ValueError("原生 Gateway 不能指定远端连接")
            if gateway_id != registry.gateway_id:
                raise ValueError("Node 宿主目标不属于原生 Gateway")
            headers.update({"X-Local-Token": get_gateway_local_token(), "X-BoxTeam-Gateway-Id": registry.gateway_id})
        base_url = urls.pop()
        management_routes = () if remote else tuple(
            BackendManagementRoute(base_url, target.system_default, tuple(leases), None,
                registry.backend_owner(target.key), target if registry.backend_owner(target.key) is None else None)
            for target in targets
        )
        yield _HostRoute(base_url, remote, headers, leases, management_routes=management_routes)


def _check_management_route(request: HTTPConnection, route: _HostRoute) -> None:
    if route.connection_id is not None:
        registry = request.app.state.registry
        current = registry.remote_gateway_route_identity(route.connection_id)
        if route.connection_identity is None or any(before is not after for before, after in zip(route.connection_identity, current, strict=True)):
            raise LookupError("远端 Gateway 管理路由已换代，请重新发现原目标")
    _check_access(None, route.leases)


@contextmanager
def _management_gateway_route(request: HTTPConnection, payload: ExtensionTargetListRequest | ExtensionInstallationInventoryRequest | ExtensionWebHostOpenRequest | ExtensionConfigurationViewRequest,
                              auth: GatewayAuthContext) -> Iterator[_HostRoute | None]:
    registry = request.app.state.registry
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise TypeError("Gateway 原 registry 尚未初始化")
    if payload.gateway_id == registry.gateway_id:
        if payload.gateway_connection_id is not None:
            raise ValueError("原生 Gateway 不能指定远端连接")
        yield None
        return
    if auth.kind == "federation":
        raise PermissionError("联邦入站请求禁止再次 Relay 到其它 Gateway")
    connections = [connection for connection in registry.remote_gateway_connections()
                   if connection.remote_gateway_id == payload.gateway_id
                   and (payload.gateway_connection_id is None or connection.connection_id == payload.gateway_connection_id)]
    if len(connections) != 1:
        raise LookupError("目标 Gateway 管理连接不能唯一确定，请明确 gateway_connection_id")
    connection = connections[0]
    credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").get(connection.connection_id)
    identity = registry.remote_gateway_route_identity(connection.connection_id)
    url = registry.acquire_remote_gateway_reference(connection.connection_id)
    try:
        route = _HostRoute(url, True, {"X-Request-ID": get_request_id(request),
            "X-BoxTeam-Federation-Token": credential.token, "X-BoxTeam-Target-Gateway-Id": payload.gateway_id,
            "X-BoxTeam-Gateway-Id": registry.gateway_id}, [], connection.connection_id, identity)
        _check_management_route(request, route)
        yield route
    finally:
        registry.release_remote_gateway_reference(connection.connection_id)


def _check_management_response(response: httpx.Response, request_id: str) -> None:
    body = response.json()
    if (response.headers.get("X-Request-ID") != request_id or not isinstance(body, dict)
        or body.get("request_id") != request_id):
        raise ValueError("扩展管理响应不属于原 request_id")


@router.post("/api/gateway/extension-targets", response_model=APIResponse[ExtensionTargetListDTO])
async def list_extension_targets(
    payload: ExtensionTargetListRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionTargetListDTO] | Response:
    try:
        with _management_gateway_route(request, payload, auth) as remote:
            if remote is not None:
                _check_access(access, [])
                response = await _http_client(request).post(f"{remote.base_url.rstrip('/')}/api/gateway/extension-targets",
                    headers=remote.headers, json=payload.model_dump(exclude={"gateway_connection_id"}), timeout=10)
                _check_management_route(request, remote)
                _check_access(access, [])
                if access is not None:
                    expires_at = _users(request).expires_at(access)
                    if expires_at is None or datetime.fromisoformat(expires_at) <= datetime.now(UTC):
                        raise PermissionError("扩展目标客户端租约已过期")
                _check_management_response(response, request_id)
                if not response.is_success:
                    return Response(response.content, status_code=response.status_code, media_type="application/json")
                result = APIResponse[ExtensionTargetListDTO].model_validate_json(response.content)
                if result.code != 0 or result.data is None or result.data.gateway_id != payload.gateway_id or result.data.protocol_version != payload.protocol_version:
                    raise ValueError("远端安装域目录不属于原 Gateway 或协议")
                return result
            capabilities = _check_inventory_permission(request, auth)
            owner = management_owner(request)
            _check_inventory_access(request, auth, access, [], capabilities)
            if access is not None:
                expires_at = _users(request).expires_at(access)
                if expires_at is None or datetime.fromisoformat(expires_at) <= datetime.now(UTC):
                    raise PermissionError("扩展目标客户端租约已过期")
            target = ExtensionTargetDTO(protocol_version=payload.protocol_version, gateway_id=payload.gateway_id,
                backend_id=owner.backend_id, capabilities=ExtensionTargetCapabilitiesDTO(
                    installation_manage=capabilities[0], host_manage=capabilities[1]))
            return APIResponse(data=ExtensionTargetListDTO(protocol_version=payload.protocol_version,
                gateway_id=payload.gateway_id, targets=[target]), request_id=request_id)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValidationError as error:
        raise HTTPException(502, f"目标安装域发现回执结构无效: {error.errors(include_input=False)}") from error
    except (LookupError, ValueError, RuntimeError) as error:
        raise HTTPException(409, str(error)) from error
    except httpx.HTTPStatusError as error:
        return Response(error.response.content, status_code=error.response.status_code, media_type="application/json")
    except httpx.RequestError as error:
        raise HTTPException(502, f"目标安装域发现失败: {error}") from error


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
                           auth=auth, streaming=False, gateway_connection_id=payload.gateway_connection_id) as route:
            _check(request, auth, access, route.leases, capability="extension_host_manage")
            path = "api/gateway/extension-hosts/resolve" if route.remote else "api/internal/extension-hosts/resolve"
            response = await _http_client(request).post(f"{route.base_url.rstrip('/')}/{path}",
                headers=route.headers, json=payload.model_dump(exclude={"gateway_connection_id"}), timeout=10)
            _check(request, auth, access, route.leases, capability="extension_host_manage")
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
            for management_route in route.management_routes:
                request.app.state.registry.confirm_backend_management_identity(management_route, target.backend_id)
            return result
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except (httpx.RequestError, ValidationError) as error:
        raise HTTPException(502, f"Node 宿主目标发现失败: {error}") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/gateway/extension-targets/resolve", response_model=APIResponse[ExtensionTargetViewDTO])
async def resolve_extension_target(
    payload: ExtensionHostTargetRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionTargetViewDTO] | Response:
    local_capabilities: tuple[bool, bool] | None = None
    current_capabilities: tuple[bool, bool] | None = None
    try:
        with _target_route(request, gateway_id=payload.gateway_id, workspace_ids=payload.workspace_ids,
                           auth=auth, streaming=False, gateway_connection_id=payload.gateway_connection_id) as route:
            _check_access(access, route.leases)
            local_capabilities = None if route.remote else _check_inventory_permission(request, auth)
            path = "api/gateway/extension-targets/resolve" if route.remote else "api/internal/extension-targets/resolve"
            response = await _http_client(request).post(f"{route.base_url.rstrip('/')}/{path}",
                headers=route.headers, json=payload.model_dump(exclude={"gateway_connection_id"}), timeout=10)
            _check_access(access, route.leases)
            if local_capabilities is not None:
                current_capabilities = _extension_capabilities(request, auth)
                if current_capabilities != local_capabilities or not any(current_capabilities):
                    raise PermissionError("扩展管理权限在目标发现期间发生变化，请重新查询")
            if access is not None:
                expires_at = _users(request).expires_at(access)
                if expires_at is None or datetime.fromisoformat(expires_at) <= datetime.now(UTC):
                    raise PermissionError("扩展目标客户端租约已过期")
            if response.headers.get("X-Request-ID") != request_id:
                raise ValueError("扩展目标 Backend 返回了不同 request_id")
            if not response.is_success:
                failure = response.json()
                if not isinstance(failure, dict) or failure.get("request_id") != request_id:
                    raise ValueError("扩展目标错误回执不属于原请求")
                return Response(response.content, status_code=response.status_code, media_type="application/json")
            if route.remote:
                result = APIResponse[ExtensionTargetViewDTO].model_validate_json(response.content)
                resolved = result.data
                if (result.code != 0 or result.request_id != request_id or resolved is None
                    or resolved.protocol_version != payload.protocol_version
                    or resolved.gateway_id != payload.gateway_id or resolved.workspace_ids != payload.workspace_ids
                    or (payload.backend_id is not None and resolved.backend_id != payload.backend_id)
                    or not (resolved.capabilities.installation_manage or resolved.capabilities.host_manage)):
                    raise ValueError("远端 Gateway 返回了不同目标或无管理能力结果")
                return result
            identity = APIResponse[ExtensionHostTargetRequest].model_validate_json(response.content)
            target = identity.data
            if (identity.code != 0 or identity.request_id != request_id or target is None
                or target.backend_id is None
                or target.protocol_version != payload.protocol_version or target.gateway_id != payload.gateway_id
                or target.workspace_ids != payload.workspace_ids
                or (payload.backend_id is not None and target.backend_id != payload.backend_id)):
                raise ValueError("扩展目标 Backend 返回了不同稳定身份或 request_id")
            if current_capabilities is None:
                raise RuntimeError("原生扩展目标权限未完成重核")
            for management_route in route.management_routes:
                request.app.state.registry.confirm_backend_management_identity(management_route, target.backend_id)
            result = ExtensionTargetViewDTO(protocol_version=target.protocol_version, gateway_id=target.gateway_id,
                workspace_ids=target.workspace_ids, backend_id=target.backend_id, source="live",
                capabilities=ExtensionTargetCapabilitiesDTO(installation_manage=current_capabilities[0],
                    host_manage=current_capabilities[1]))
            return APIResponse(data=result, request_id=request_id)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except httpx.RequestError as error:
        raise HTTPException(502, f"扩展目标发现失败: {error}") from error
    except ValidationError as error:
        raise HTTPException(502, f"扩展目标发现回执结构无效: {error.errors(include_input=False)}") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/gateway/extension-installations/inventory", response_model=APIResponse[ExtensionInstallationInventoryDTO])
async def extension_installation_inventory(
    payload: ExtensionInstallationInventoryRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionInstallationInventoryDTO] | Response:
    return await _installation_request(payload, request, auth, access, request_id,
        operation="inventory", response_type=APIResponse[ExtensionInstallationInventoryDTO])


@router.post("/api/gateway/extension-installations/install", response_model=APIResponse[ExtensionInstallationInstallDTO])
async def extension_installation_install(
    payload: ExtensionInstallationInstallRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionInstallationInstallDTO] | Response:
    return await _installation_request(payload, request, auth, access, request_id,
        operation="install", response_type=APIResponse[ExtensionInstallationInstallDTO])


@router.post("/api/gateway/extension-installations/uninstall", response_model=APIResponse[ExtensionInstallationUninstallDTO])
async def extension_installation_uninstall(
    payload: ExtensionInstallationUninstallRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionInstallationUninstallDTO] | Response:
    return await _installation_request(payload, request, auth, access, request_id,
        operation="uninstall", response_type=APIResponse[ExtensionInstallationUninstallDTO])


InstallationResult = TypeVar("InstallationResult", bound=ExtensionInstallationInventoryDTO)


@contextmanager
def _installation_route(request: HTTPConnection, payload: ExtensionInstallationInventoryRequest | ExtensionWebHostOpenRequest | ExtensionConfigurationViewRequest,
                        auth: GatewayAuthContext, *, streaming: bool = False) -> Iterator[_HostRoute]:
    with _management_gateway_route(request, payload, auth) as remote:
        if remote is not None:
            yield remote
            return
        _check_inventory_permission(request, auth)
        if isinstance(payload, (ExtensionWebHostOpenRequest, ExtensionConfigurationViewRequest)) and payload.workspace_ids:
            # 非空运行视图沿原挂接根路由；直连 Backend 不必具有 Launcher 进程组 owner。
            with _target_route(request, gateway_id=payload.gateway_id, workspace_ids=payload.workspace_ids,
                auth=auth, streaming=streaming, gateway_connection_id=payload.gateway_connection_id) as route:
                yield route
            return
        needs_backend = isinstance(payload, (ExtensionInstallationInstallRequest, ExtensionWebHostOpenRequest)) or getattr(payload, "generation", None) is not None
        if not needs_backend:
            management_owner(request, payload.backend_id)
            yield _HostRoute("", False, {}, [])
            return
        workspace_ids = tuple(payload.workspace_ids) if isinstance(payload,
            (ExtensionInstallationInstallRequest, ExtensionWebHostOpenRequest, ExtensionConfigurationViewRequest)) else ()
        with request.app.state.registry.backend_management_routes(backend_id=payload.backend_id, workspace_ids=workspace_ids, streaming=streaming) as routes:
            [selected] = routes
            yield _HostRoute(selected.base_url, False, {"X-Request-ID": get_request_id(request),
                "X-Local-Token": get_gateway_local_token(), "X-BoxTeam-Gateway-Id": payload.gateway_id}, list(selected.leases))


async def _installation_request(
    payload: ExtensionInstallationInventoryRequest, request: Request, auth: GatewayAuthContext,
    access: UserAccessContext | None, request_id: str, *,
    operation: Literal["inventory", "install", "uninstall"], response_type: type[APIResponse[InstallationResult]],
) -> APIResponse[InstallationResult] | Response:
    if operation != "inventory":
        require_federation_capability(request, auth, "extension_installation_manage")
    label = {"install": "扩展安装", "uninstall": "扩展卸载", "inventory": "扩展库存"}[operation]
    try:
        with _installation_route(request, payload, auth) as route:
            if operation == "inventory":
                expected_capabilities = _check_inventory_permission(request, auth)
                _check_inventory_access(request, auth, access, route.leases, expected_capabilities)
            else:
                expected_capabilities = None
                _check(request, auth, access, route.leases, capability="extension_installation_manage")
            if not route.remote and operation != "install":
                owner = management_owner(request, payload.backend_id)
                if operation == "inventory":
                    inventory = await owner.call({"method": "inventory"}, response_type=ProfileExtensionInventory)
                    publication = ExtensionInstallationInventoryDTO(**payload.model_dump(exclude={"gateway_connection_id"}), inventory=inventory)
                else:
                    assert isinstance(payload, ExtensionInstallationUninstallRequest)
                    removed = await owner.call({"method": "uninstall", "profile_id": payload.profile_id,
                        "extension_id": payload.extension_id, "version": payload.version}, response_type=UninstalledProfile)
                    publication = ExtensionInstallationUninstallDTO(**payload.model_dump(exclude={"gateway_connection_id", "profile_id", "extension_id", "version"}),
                        uninstalled=removed.uninstalled, inventory=removed.inventory)
                result = response_type.model_validate({"data": publication.model_dump(), "request_id": request_id})
            else:
                prefix = "api/gateway" if route.remote else "api/internal"
                path = f"{prefix}/extension-installations/{operation}"
                result = await _http_client(request).post(f"{route.base_url.rstrip('/')}/{path}",
                    headers=route.headers, json=payload.model_dump(exclude={"gateway_connection_id"}), timeout=120)
            # 读取期间撤权、换路由或租约过期时，库存不能再披露给原请求者。
            _check_management_route(request, route)
            if expected_capabilities is not None:
                _check_inventory_access(request, auth, access, route.leases, expected_capabilities)
            else:
                _check(request, auth, access, route.leases, capability="extension_installation_manage")
            if access is not None:
                expires_at = _users(request).expires_at(access)
                if expires_at is None or datetime.fromisoformat(expires_at) <= datetime.now(UTC):
                    raise PermissionError(f"{label}客户端租约已过期")
            if isinstance(result, httpx.Response):
                _check_management_response(result, request_id)
                if not result.is_success:
                    return Response(result.content, status_code=result.status_code, media_type="application/json")
                result = response_type.model_validate_json(result.content)
            target = result.data
            if (result.code != 0 or result.request_id != request_id or target is None
                or target.model_dump(include=set(ExtensionInstallationTarget.model_fields))
                != payload.model_dump(include=set(ExtensionInstallationTarget.model_fields))
                or isinstance(payload, ExtensionInstallationInstallRequest)
                and (not isinstance(target, ExtensionInstallationInstallDTO) or target.workspace_ids != payload.workspace_ids)):
                raise ValueError(f"{label}返回了不同目标或请求身份")
            if isinstance(payload, ExtensionInstallationUninstallRequest) and (
                not isinstance(target, ExtensionInstallationUninstallDTO)
                or target.uninstalled.id.lower() != payload.extension_id.lower()
                or target.uninstalled.version != payload.version
                or target.uninstalled.profile_id != payload.profile_id
                or target.inventory.profile_id != payload.profile_id
            ):
                raise ValueError("扩展卸载回执与原 profile 成员不匹配")
            return result
    except ExtensionManagementError as error:
        raise HTTPException(409 if error.code in {"Invalid", "InstalledExtensionNotFound"} else 400, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except httpx.HTTPStatusError as error:
        return Response(error.response.content, status_code=error.response.status_code, media_type="application/json")
    except httpx.RequestError as error:
        detail = (f"目标扩展库存读取失败: {error}" if operation == "inventory"
                  else f"{label}回执未确认，请先查询原目标库存: {error}")
        raise HTTPException(502, detail) from error
    except ValidationError as error:
        raise HTTPException(502, f"目标{label}回执结构无效: {error.errors(include_input=False)}") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except RuntimeError as error:
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
        _check(websocket, auth, access, leases, capability="extension_host_manage")
        if not isinstance(data, bytes):
            raise TypeError("宿主握手后只接受原协议二进制帧")
        if outbound:
            await upstream.send(data)
        else:
            await websocket.send_bytes(data)


async def _upstream(websocket: WebSocket, *, url: str, headers: dict[str, str], payload: ExtensionHostOpenRequest | ExtensionWebHostOpenRequest,
                    auth: GatewayAuthContext, access: UserAccessContext | None,
                    leases: list[WorkspaceRouteLease], expected_client_id: str) -> None:
    async with connect(url, additional_headers=headers, compression=None,
                       open_timeout=10, close_timeout=10) as upstream:
        _check(websocket, auth, access, leases, capability="extension_host_manage")
        await upstream.send(payload.model_dump_json(exclude={"gateway_connection_id"}))
        async with asyncio.timeout(15):
            raw = await upstream.recv()
        if not isinstance(raw, str):
            raise TypeError("Node 宿主缺少通道身份握手")
        binding_type = ExtensionWebHostConnectionDTO if isinstance(payload, ExtensionWebHostOpenRequest) else ExtensionHostConnectionDTO
        binding = binding_type.model_validate_json(raw)
        fields = set(type(payload).model_fields) - {"gateway_connection_id"}
        if (binding.model_dump(include=fields) != payload.model_dump(include=fields)
            or binding.request_id != get_request_id(websocket) or binding.client_id != expected_client_id):
            raise ValueError("Node 宿主返回了不同目标、视图或请求身份")
        _check(websocket, auth, access, leases, capability="extension_host_manage")
        await websocket.send_text(binding.model_dump_json())
        await run_channel(
            _relay(websocket, upstream, outbound=True, auth=auth, access=access, leases=leases),
            _relay(websocket, upstream, outbound=False, auth=auth, access=access, leases=leases))


async def _watch_management_route(websocket: WebSocket, route: _HostRoute) -> None:
    registry = websocket.app.state.registry
    changed = asyncio.Event()
    registry.add_commit_observer(changed.set)
    try:
        while True:
            changed.clear()
            _check_management_route(websocket, route)
            await changed.wait()
    finally:
        registry.remove_commit_observer(changed.set)


@router.websocket("/api/gateway/extension-web-hosts/channel")
async def extension_web_host_channel(websocket: WebSocket) -> None:
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
            payload = ExtensionWebHostOpenRequest.model_validate_json(await websocket.receive_text())
        with _installation_route(websocket, payload, auth, streaming=True) as route:
            _check(websocket, auth, access, route.leases, capability="extension_host_manage")
            headers = {**route.headers, CLIENT_HEADER: client_id}
            expected_client_id = f"peer:{websocket.app.state.registry.gateway_id}:{client_id}" if route.remote else client_id
            path = "api/gateway/extension-web-hosts/channel" if route.remote else "api/internal/extension-web-hosts/channel"
            operations = [
                _upstream(websocket, url=_websocket_target(route.base_url, path), headers=headers, payload=payload,
                    auth=auth, access=access, leases=route.leases, expected_client_id=expected_client_id),
                _watch_policy(websocket, auth), _watch_management_route(websocket, route),
                *(_watch_route(lease) for lease in route.leases),
            ]
            if access is not None:
                operations.append(_watch_user(websocket, access))
            await run_channel(*operations)
        await close_channel(websocket)
    except WebSocketDisconnect:
        return
    except ConnectionClosed as error:
        await close_channel(websocket, code=1000 if error.rcvd is not None and error.rcvd.code == 1000 else 1011,
                            reason="extension_web_host_upstream_closed")
    except Exception:
        logger.exception("Gateway Web 宿主通道失败: request_id=%s", request_id)
        await close_channel(websocket, code=1011, reason="extension_web_host_channel_failed")


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
            _check(websocket, auth, access, leases, capability="extension_host_manage")
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
