"""精确 Gateway／Backend 的 MCP 制品管理代理；不持有安装或工作区业务事实。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, TypeVar
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError

from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    get_gateway_local_token,
    verify_gateway_access,
)
from app.gateway.control.user_access import UserAccessContext, UserAccessService
from app.gateway.federation.policy import require_federation_capability
from app.gateway.server.management_routes import (
    GatewayManagementRoute,
    check_management_route,
    management_gateway_route,
)
from app.gateway.server.workspace_proxy import (
    _http_client,
    verify_user_access_for_proxy,
)
from app.schemas.canonical_params import CanonicalGatewayId
from app.schemas.gateway.extension_host import BackendId
from app.schemas.internal_v2.mcp import (
    McpArtifactReferencesResponseDTO,
    McpCandidateInspectRequestDTO,
    McpCandidateInspectResponseDTO,
    McpCandidatePlanRequestDTO,
    McpCandidatePlanResponseDTO,
    McpCandidatePlanValidationRequestDTO,
    McpInstallationCapabilitiesResponseDTO,
    McpInstallationOperationResponseDTO,
    McpInstallationRequestDTO,
    McpInstallationTargetDTO,
    McpInstallationTargetListRequestDTO,
    McpInstallationTargetListResponseDTO,
    McpInstalledArtifactListResponseDTO,
    McpUninstallationRequestDTO,
)

Auth = Annotated[GatewayAuthContext, Depends(verify_gateway_access)]
Access = Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)]
Result = TypeVar("Result", McpCandidateInspectResponseDTO, McpCandidatePlanResponseDTO,
    McpInstallationCapabilitiesResponseDTO, McpInstallationOperationResponseDTO,
    McpInstalledArtifactListResponseDTO, McpArtifactReferencesResponseDTO)
Payload = McpCandidateInspectRequestDTO | McpCandidatePlanRequestDTO | McpCandidatePlanValidationRequestDTO | McpInstallationRequestDTO | McpInstallationTargetDTO | McpUninstallationRequestDTO


def _check_access(request: Request, auth: GatewayAuthContext, access: UserAccessContext | None, route: GatewayManagementRoute) -> None:
    require_federation_capability(request, auth, "mcp_installation_manage")
    check_management_route(request, route)
    if access is not None:
        users = request.app.state.user_access_service
        if not isinstance(users, UserAccessService):
            raise TypeError("Gateway 用户访问服务尚未初始化")
        expires = users.expires_at(access)
        if access.invalidated.is_set() or expires is None or datetime.fromisoformat(expires) <= datetime.now(UTC):
            raise PermissionError("MCP 管理请求的用户访问租约已失效")


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (httpx.RequestError, ValidationError) as error:
        raise HTTPException(502, "目标 MCP 管理回执未确认；请查询原操作，不自动重放") from error
    except (LookupError, ValueError) as error:
        raise HTTPException(409, str(error)) from error


def _check_response(response: httpx.Response, request_id: str) -> None:
    data = response.json()
    if response.headers.get("X-Request-ID") != request_id or not isinstance(data, dict) or data.get("request_id") != request_id:
        raise ValueError("MCP 管理响应不属于原 request_id")


def _permission(request: Request, auth: Auth) -> None:
    require_federation_capability(request, auth, "mcp_installation_manage")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(_permission), Depends(_no_store)])


@router.post("/api/gateway/mcp-installation-targets", response_model=McpInstallationTargetListResponseDTO)
async def list_installation_targets(payload: McpInstallationTargetListRequestDTO, request: Request, auth: Auth, access: Access) -> McpInstallationTargetListResponseDTO | Response:
    with _errors(), management_gateway_route(request, gateway_id=payload.gateway_id,
        gateway_connection_id=payload.gateway_connection_id, auth=auth) as remote:
        request_id = get_request_id(request)
        if remote is not None:
            _check_access(request, auth, access, remote)
            response = await _http_client(request).post(remote.base_url.rstrip('/') + '/api/gateway/mcp-installation-targets',
                headers=remote.headers, json=payload.model_dump(exclude={'gateway_connection_id'}), timeout=10)
            _check_access(request, auth, access, remote)
            _check_response(response, request_id)
            if not response.is_success:
                return Response(response.content, status_code=response.status_code, media_type="application/json")
            result = McpInstallationTargetListResponseDTO.model_validate_json(response.content)
            if result.gateway_id != payload.gateway_id or any(target.gateway_id != payload.gateway_id for target in result.data):
                raise ValueError("MCP 安装域发现返回了不同 Gateway")
            return result
        require_federation_capability(request, auth, "mcp_installation_manage")
        registry = request.app.state.registry
        targets: dict[str, McpInstallationTargetDTO] = {}
        with registry.backend_management_routes() as routes:
            for registered in routes:
                route = GatewayManagementRoute(registered.base_url, False, {}, list(registered.leases))
                _check_access(request, auth, access, route)
                if registered.backend_id in targets:
                    continue
                response = await _http_client(request).get(registered.base_url.rstrip('/') + '/api/v1/mcp/installation-capabilities',
                    headers={'X-Request-ID': request_id, 'X-Local-Token': get_gateway_local_token(), 'X-BoxTeam-Gateway-Id': payload.gateway_id}, timeout=10)
                _check_access(request, auth, access, route)
                _check_response(response, request_id)
                if not response.is_success:
                    return Response(response.content, status_code=response.status_code, media_type="application/json")
                result = McpInstallationCapabilitiesResponseDTO.model_validate_json(response.content)
                target = result.target
                if result.code != 0 or target.gateway_id != payload.gateway_id or registered.backend_id is not None and registered.backend_id != target.backend_id:
                    raise ValueError("Backend 安装域发现与原登记身份不一致")
                registry.confirm_backend_management_identity(registered, target.backend_id)
                targets[target.backend_id] = target
        return McpInstallationTargetListResponseDTO(data=list(targets.values()), gateway_id=payload.gateway_id, request_id=request_id)


async def _forward(request: Request, auth: GatewayAuthContext, access: UserAccessContext | None,
    gateway_id: str, backend_id: str, gateway_connection_id: str | None, *, suffix: str,
    response_type: type[Result], payload: Payload | None = None,
) -> Result | Response:
    target = McpInstallationTargetDTO(gateway_id=gateway_id, backend_id=backend_id)
    with _errors(), management_gateway_route(request, gateway_id=gateway_id,
        gateway_connection_id=gateway_connection_id, auth=auth) as remote:
        if payload is not None:
            original = payload.candidate.target if isinstance(payload, (McpInstallationRequestDTO, McpCandidatePlanValidationRequestDTO)) else payload if isinstance(payload, McpInstallationTargetDTO) else payload.target
            if original != target:
                raise ValueError("MCP 管理正文与请求的稳定 Gateway／Backend 不一致")
        if remote is not None:
            return await _send(request, auth, access, remote, suffix=f"api/gateway/mcp/{gateway_id}/{backend_id}/{suffix}",
                target=target, response_type=response_type, payload=payload)
        require_federation_capability(request, auth, "mcp_installation_manage")
        with request.app.state.registry.backend_management_routes(backend_id=backend_id) as routes:
            [registered] = routes
            route = GatewayManagementRoute(registered.base_url, False,
                {'X-Local-Token': get_gateway_local_token(), 'X-BoxTeam-Gateway-Id': gateway_id, 'X-Request-ID': get_request_id(request)}, list(registered.leases))
            return await _send(request, auth, access, route, suffix=f"api/v1/mcp/{suffix}",
                target=target, response_type=response_type, payload=payload)


async def _send(request: Request, auth: GatewayAuthContext, access: UserAccessContext | None,
    route: GatewayManagementRoute, *, suffix: str, target: McpInstallationTargetDTO, response_type: type[Result], payload: Payload | None,
) -> Result | Response:
    _check_access(request, auth, access, route)
    response = await _http_client(request).request(request.method, route.base_url.rstrip('/') + '/' + suffix,
        headers=route.headers, json=payload.model_dump(mode='json') if payload is not None else None, timeout=60)
    _check_access(request, auth, access, route)
    _check_response(response, get_request_id(request))
    if not response.is_success:
        return Response(response.content, status_code=response.status_code, media_type="application/json", headers={'Cache-Control': 'no-store'})
    result = response_type.model_validate_json(response.content)
    if result.code != 0 or result.target != target:
        raise ValueError("MCP 管理回执与冻结目标不一致")
    return result


@router.get("/api/gateway/mcp/{gateway_id}/{backend_id}/installation-capabilities", response_model=McpInstallationCapabilitiesResponseDTO)
async def capabilities(gateway_id: CanonicalGatewayId, backend_id: BackendId, request: Request, auth: Auth, access: Access, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpInstallationCapabilitiesResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix='installation-capabilities', response_type=McpInstallationCapabilitiesResponseDTO)


@router.post("/api/gateway/mcp/{gateway_id}/{backend_id}/candidate-manifests", response_model=McpCandidateInspectResponseDTO)
async def inspect_candidate(gateway_id: CanonicalGatewayId, backend_id: BackendId, request: Request, auth: Auth, access: Access, payload: McpCandidateInspectRequestDTO, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpCandidateInspectResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix='candidate-manifests', response_type=McpCandidateInspectResponseDTO, payload=payload)


@router.post("/api/gateway/mcp/{gateway_id}/{backend_id}/candidate-plans", response_model=McpCandidatePlanResponseDTO)
async def plan_candidate(gateway_id: CanonicalGatewayId, backend_id: BackendId, request: Request, auth: Auth, access: Access, payload: McpCandidatePlanRequestDTO, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpCandidatePlanResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix='candidate-plans', response_type=McpCandidatePlanResponseDTO, payload=payload)


@router.post("/api/gateway/mcp/{gateway_id}/{backend_id}/candidate-plans/validate", response_model=McpCandidatePlanResponseDTO)
async def validate_candidate(gateway_id: CanonicalGatewayId, backend_id: BackendId, request: Request, auth: Auth, access: Access, payload: McpCandidatePlanValidationRequestDTO, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpCandidatePlanResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix='candidate-plans/validate', response_type=McpCandidatePlanResponseDTO, payload=payload)


@router.post("/api/gateway/mcp/{gateway_id}/{backend_id}/installations", response_model=McpInstallationOperationResponseDTO)
async def install(gateway_id: CanonicalGatewayId, backend_id: BackendId, request: Request, auth: Auth, access: Access, payload: McpInstallationRequestDTO, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpInstallationOperationResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix='installations', response_type=McpInstallationOperationResponseDTO, payload=payload)


@router.get("/api/gateway/mcp/{gateway_id}/{backend_id}/installations", response_model=McpInstalledArtifactListResponseDTO)
async def inventory(gateway_id: CanonicalGatewayId, backend_id: BackendId, request: Request, auth: Auth, access: Access, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpInstalledArtifactListResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix='installations', response_type=McpInstalledArtifactListResponseDTO)


@router.get("/api/gateway/mcp/{gateway_id}/{backend_id}/installation-operations/{operation_id}", response_model=McpInstallationOperationResponseDTO)
async def operation(gateway_id: CanonicalGatewayId, backend_id: BackendId, operation_id: str, request: Request, auth: Auth, access: Access, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpInstallationOperationResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix=f'installation-operations/{quote(operation_id, safe="")}', response_type=McpInstallationOperationResponseDTO)


@router.post("/api/gateway/mcp/{gateway_id}/{backend_id}/installation-operations/{operation_id}/cancel", response_model=McpInstallationOperationResponseDTO)
async def cancel(gateway_id: CanonicalGatewayId, backend_id: BackendId, operation_id: str, request: Request, auth: Auth, access: Access, payload: McpInstallationTargetDTO, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpInstallationOperationResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix=f'installation-operations/{quote(operation_id, safe="")}/cancel', response_type=McpInstallationOperationResponseDTO, payload=payload)


@router.get("/api/gateway/mcp/{gateway_id}/{backend_id}/installations/{installation_id}/references", response_model=McpArtifactReferencesResponseDTO)
async def references(gateway_id: CanonicalGatewayId, backend_id: BackendId, installation_id: str, request: Request, auth: Auth, access: Access, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpArtifactReferencesResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix=f'installations/{quote(installation_id, safe="")}/references', response_type=McpArtifactReferencesResponseDTO)


@router.delete("/api/gateway/mcp/{gateway_id}/{backend_id}/installations/{installation_id}", response_model=McpInstallationOperationResponseDTO)
async def uninstall(gateway_id: CanonicalGatewayId, backend_id: BackendId, installation_id: str, request: Request, auth: Auth, access: Access, payload: McpUninstallationRequestDTO, gateway_connection_id: str | None = Query(default=None, min_length=1)) -> McpInstallationOperationResponseDTO | Response:
    return await _forward(request, auth, access, gateway_id, backend_id, gateway_connection_id,
        suffix=f'installations/{quote(installation_id, safe="")}', response_type=McpInstallationOperationResponseDTO, payload=payload)
