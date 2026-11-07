"""管理请求复用原 registry 的路由与引用；连接选择只在当前 Gateway 消费。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from starlette.requests import HTTPConnection

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import get_request_id
from app.gateway.auth import GatewayAuthContext
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceRouteLease
from app.gateway.registry.routes import BackendManagementRoute


@dataclass(frozen=True, slots=True)
class GatewayManagementRoute:
    base_url: str
    remote: bool
    headers: dict[str, str]
    leases: list[WorkspaceRouteLease]
    connection_id: str | None = None
    connection_identity: tuple[object, object] | None = None
    management_routes: tuple[BackendManagementRoute, ...] = ()


def check_management_route(request: HTTPConnection, route: GatewayManagementRoute) -> None:
    if route.connection_id is not None:
        registry = request.app.state.registry
        current = registry.remote_gateway_route_identity(route.connection_id)
        if route.connection_identity is None or any(before is not after for before, after in zip(route.connection_identity, current, strict=True)):
            raise LookupError("远端 Gateway 管理路由已换代，请重新发现原目标")
    if any(lease.invalidated.is_set() for lease in route.leases):
        raise LookupError("Backend 管理路由已失效")


@contextmanager
def management_gateway_route(request: HTTPConnection, *, gateway_id: str,
                             gateway_connection_id: str | None, auth: GatewayAuthContext) -> Iterator[GatewayManagementRoute | None]:
    registry = request.app.state.registry
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise TypeError("Gateway 原 registry 尚未初始化")
    if gateway_id == registry.gateway_id:
        if gateway_connection_id is not None:
            raise ValueError("原生 Gateway 不能指定远端连接")
        yield None
        return
    if auth.kind == "federation":
        raise PermissionError("联邦入站请求禁止再次 Relay 到其它 Gateway")
    connections = [connection for connection in registry.remote_gateway_connections()
                   if connection.remote_gateway_id == gateway_id
                   and (gateway_connection_id is None or connection.connection_id == gateway_connection_id)]
    if len(connections) != 1:
        raise LookupError("目标 Gateway 管理连接不能唯一确定，请明确 gateway_connection_id")
    connection = connections[0]
    credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").get(connection.connection_id)
    identity = registry.remote_gateway_route_identity(connection.connection_id)
    url = registry.acquire_remote_gateway_reference(connection.connection_id)
    try:
        route = GatewayManagementRoute(url, True, {"X-Request-ID": get_request_id(request),
            "X-BoxTeam-Federation-Token": credential.token, "X-BoxTeam-Target-Gateway-Id": gateway_id,
            "X-BoxTeam-Gateway-Id": registry.gateway_id}, [], connection.connection_id, identity)
        check_management_route(request, route)
        yield route
    finally:
        registry.release_remote_gateway_reference(connection.connection_id)
