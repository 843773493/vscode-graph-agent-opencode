"""目标侧命名密钥管理；明文解析只提供给受信本机服务通道。"""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.runtime_control import verify_local_control_request
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.federation.policy import require_federation_capability
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER, parse_target_gateway_id
from app.gateway.secrets.vault import SecretConflictError, SecretVault
from app.gateway.server.management_routes import (
    check_management_route,
    management_gateway_route,
)
from app.schemas.gateway.secrets import (
    CreateSecretRequest,
    GatewaySecretDTO,
    GatewaySecretsDTO,
    ResolveSecretsRequest,
    SecretId,
    UpdateSecretRequest,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


def get_secret_vault(request: Request) -> SecretVault:
    return request.app.state.secret_vault


async def _relay(request: Request, auth: GatewayAuthContext, connection_id: str | None) -> Response | None:
    gateway_id = parse_target_gateway_id(request.headers.get(TARGET_GATEWAY_ID_HEADER)) or request.app.state.gateway_id
    try:
        with management_gateway_route(request, gateway_id=gateway_id, gateway_connection_id=connection_id, auth=auth) as route:
            if route is None:
                return None
            query = urlencode([(key, value) for key, value in request.query_params.multi_items() if key != "gateway_connection_id"])
            response = await request.app.state.http_client.request(request.method,
                route.base_url + request.url.path + ("?" + query if query else ""),
                content=await request.body(), headers={**route.headers, "Content-Type": "application/json"}, timeout=15)
            try:
                # 已发出的远端写入可能生效；回执失效不能报告为写入前的拒绝。
                check_management_route(request, route)
                if response.headers.get("X-Request-ID") != get_request_id(request):
                    raise HTTPException(502, "密钥管理回执的 request_id 不匹配")
                if response.is_success:
                    payload = response.json()
                    model = GatewaySecretDTO if request.method == "POST" else GatewaySecretsDTO
                    if payload.get("request_id") != get_request_id(request) or model.model_validate(payload["data"]).gateway_id != gateway_id:
                        raise HTTPException(502, "密钥管理回执不属于原目标")
            except (LookupError, ValueError, TypeError) as error:
                raise HTTPException(502, "密钥管理原目标回执无效；写入结果未知，请读取原目标元数据") from error
            return Response(response.content, status_code=response.status_code,
                headers={"Content-Type": "application/json", "Cache-Control": "no-store"})
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, "密钥管理目标或回执无效") from error


async def _manage(request: Request, auth: GatewayAuthContext, connection_id: str | None, vault: SecretVault,
                  action: str, secret_id: str | None = None, payload: CreateSecretRequest | UpdateSecretRequest | None = None,
                  expected_revision: int | None = None) -> APIResponse[GatewaySecretsDTO] | APIResponse[GatewaySecretDTO] | Response:
    remote = await _relay(request, auth, connection_id)
    if remote is not None:
        return remote
    require_federation_capability(request, auth, "configuration_read" if action == "list" else "configuration_write")

    def operate():
        if isinstance(payload, CreateSecretRequest):
            return vault.create(secret_id=payload.id, name=payload.name, value=payload.value.get_secret_value())
        if isinstance(payload, UpdateSecretRequest):
            return vault.update(secret_id, expected_revision=payload.expected_revision, name=payload.name,
                                value=payload.value.get_secret_value() if payload.value is not None else None)
        if action == "delete":
            return vault.delete(secret_id, expected_revision=expected_revision)
        return vault.list()

    try:
        items = await run_sync_shielded(operate)
    except KeyError as error:
        raise HTTPException(404, "命名密钥不存在") from error
    except SecretConflictError as error:
        raise HTTPException(409, str(error)) from error
    if isinstance(payload, CreateSecretRequest):
        return APIResponse(data=GatewaySecretDTO(gateway_id=request.app.state.gateway_id, item=items), request_id=get_request_id(request))
    return APIResponse(data=GatewaySecretsDTO(gateway_id=request.app.state.gateway_id, items=items), request_id=get_request_id(request))


@router.get("/api/gateway/secrets", response_model=APIResponse[GatewaySecretsDTO])
async def list_secrets(request: Request, response: Response, auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
                       vault: Annotated[SecretVault, Depends(get_secret_vault)], gateway_connection_id: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    return await _manage(request, auth, gateway_connection_id, vault, "list")


@router.post("/api/gateway/secrets", response_model=APIResponse[GatewaySecretDTO])
async def create_secret(request: Request, response: Response, payload: CreateSecretRequest,
                        auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
                        vault: Annotated[SecretVault, Depends(get_secret_vault)], gateway_connection_id: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    return await _manage(request, auth, gateway_connection_id, vault, "create", payload=payload)


@router.put("/api/gateway/secrets/{secret_id}", response_model=APIResponse[GatewaySecretsDTO])
async def update_secret(request: Request, response: Response, secret_id: SecretId, payload: UpdateSecretRequest,
                        auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
                        vault: Annotated[SecretVault, Depends(get_secret_vault)], gateway_connection_id: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    return await _manage(request, auth, gateway_connection_id, vault, "update", secret_id=secret_id, payload=payload)


@router.delete("/api/gateway/secrets/{secret_id}", response_model=APIResponse[GatewaySecretsDTO])
async def delete_secret(request: Request, response: Response, secret_id: SecretId,
                        auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
                        vault: Annotated[SecretVault, Depends(get_secret_vault)],
                        expected_revision: int = Query(ge=1), gateway_connection_id: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    return await _manage(request, auth, gateway_connection_id, vault, "delete", secret_id=secret_id, expected_revision=expected_revision)


@router.post("/api/gateway/internal/secrets/resolve", include_in_schema=False,
             dependencies=[Depends(verify_gateway_token), Depends(verify_local_control_request)])
async def resolve_secrets(request: Request, payload: ResolveSecretsRequest,
                          vault: Annotated[SecretVault, Depends(get_secret_vault)]):
    try:
        resolved = await run_sync_shielded(vault.resolve, payload.ids)
    except KeyError as error:
        raise HTTPException(404, f"命名密钥不存在: {error.args[0]}") from error
    # SecretStr 的普通序列化始终遮罩；仅受信服务的这一出口显式解包。
    return Response(content=APIResponse(data={"gateway_id": request.app.state.gateway_id,
        "items": {key: {"revision": item.revision, "value": item.value.get_secret_value()} for key, item in resolved.items()}},
        request_id=get_request_id(request)).model_dump_json(), media_type="application/json", headers={"Cache-Control": "no-store"})
