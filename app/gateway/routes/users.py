"""Gateway 用户与访问租约路由。"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)

from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    get_gateway_local_token,
    verify_gateway_token,
)
from app.gateway.config import GatewayConfig
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessContext,
    UserAccessService,
    UserLeaseOccupiedError,
)
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.control.view_state import (
    UserViewStateRecord,
    UserViewStateStore,
)
from app.gateway.routes._shared import (
    _current_user_access,
    get_user_access_service,
    get_user_profile_store,
)
from app.schemas.gateway import (
    AcquireGatewayUserRequest,
    CreateGatewayGuestRequest,
    CreateGatewayUserRequest,
    GatewayUserAccessDTO,
    GatewayUserDTO,
    GatewayUserLeaseDTO,
    GatewayUserListDTO,
    GatewayUserViewStateDTO,
    GatewayUserViewStateUpdateRequest,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


def get_user_view_state_store(request: Request) -> UserViewStateStore:
    store = getattr(request.app.state, "user_view_state_store", None)
    if not isinstance(store, UserViewStateStore):
        raise RuntimeError("Gateway 用户视图状态存储尚未初始化")
    return store


def _user_access_dto(
    context: UserAccessContext,
    service: UserAccessService,
    *,
    takeover: bool = False,
) -> GatewayUserAccessDTO:
    return GatewayUserAccessDTO(
        kind=context.kind,  # type: ignore[arg-type]
        user_id=context.user_id,
        lease_generation=context.lease_generation,
        expires_at=service.expires_at(context),
        takeover=takeover,
    )


def _set_user_access_cookie(response: Response, context: UserAccessContext) -> None:
    response.set_cookie(
        key=USER_ACCESS_COOKIE_NAME,
        value=context.access_session_id,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _release_replaced_user_access(
    request: Request,
    service: UserAccessService,
    replacement: UserAccessContext,
) -> None:
    previous = service.resolve_cookie(request.cookies.get(USER_ACCESS_COOKIE_NAME))
    if previous is None or previous.access_session_id == replacement.access_session_id:
        return
    service.release(previous)


def _view_state_dto(record: UserViewStateRecord) -> GatewayUserViewStateDTO:
    return GatewayUserViewStateDTO(
        user_id=record.user_id,
        workspace_id=record.workspace_id,
        session_id=record.session_id,
        turn_anchor=record.turn_anchor,
        scroll_offset=record.scroll_offset,
        follow_latest=record.follow_latest,
        projection_version=record.projection_version,
        tool_details_expanded=record.tool_details_expanded,
        updated_at=record.updated_at,
    )


@router.get("/api/gateway/auth/local-credential")
async def local_credential(
    request: Request,
    request_id: str = Depends(get_request_id),
):
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site not in {None, "same-origin", "same-site"}:
        raise HTTPException(
            status_code=403,
            detail="Gateway 本地凭据只允许同站点 Web UI 获取",
        )
    return APIResponse(
        data={"token": get_gateway_local_token()},
        request_id=request_id,
    )


@router.get("/api/gateway/users", response_model=APIResponse[GatewayUserListDTO])
async def list_gateway_users(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    return APIResponse(
        data=GatewayUserListDTO(
            items=[
                GatewayUserDTO(
                    user_id=record.user.user_id,
                    display_name=record.user.display_name,
                    created_at=record.user.created_at,
                    lease=GatewayUserLeaseDTO(
                        occupied=record.lease.occupied,
                        client_label=record.lease.client_label,
                        heartbeat_at=record.lease.heartbeat_at,
                        expires_at=record.lease.expires_at,
                    ),
                )
                for record in service.list_users()
            ]
        ),
        request_id=request_id,
    )


@router.post("/api/gateway/users", response_model=APIResponse[GatewayUserDTO])
async def create_gateway_user(
    payload: CreateGatewayUserRequest,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        user = service.create_user(
            display_name=payload.display_name,
            user_id=payload.user_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    try:
        gateway_config = getattr(request.app.state, "gateway_config", None)
        initial_custom_themes = (
            gateway_config.custom_themes
            if isinstance(gateway_config, GatewayConfig)
            else ()
        )
        profiles.ensure_user(
            user_id=user.user_id,
            display_name=user.display_name,
            initial_custom_themes=initial_custom_themes,
        )
    except (OSError, ValueError) as error:
        # 用户记录没有租约，创建 profile 失败时可以安全回滚，避免产生半成品用户。
        service.delete_user(user.user_id)
        raise HTTPException(
            status_code=500, detail=f"用户 profile 初始化失败: {error}"
        ) from error
    return APIResponse(
        data=GatewayUserDTO(
            user_id=user.user_id,
            display_name=user.display_name,
            created_at=user.created_at,
        ),
        request_id=request_id,
    )


@router.delete("/api/gateway/users/{user_id}")
async def delete_gateway_user(
    user_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        service.delete_user(user_id)
    except UserLeaseOccupiedError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "user_lease_occupied",
                "client_label": error.summary.client_label,
                "expires_at": error.summary.expires_at,
            },
        ) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    try:
        profiles.delete_user(user_id=user_id)
    except OSError as error:
        raise HTTPException(
            status_code=500, detail=f"用户 profile 删除失败: {error}"
        ) from error
    return APIResponse(data={"user_id": user_id}, request_id=request_id)


@router.post(
    "/api/gateway/users/{user_id}/access",
    response_model=APIResponse[GatewayUserAccessDTO],
)
async def acquire_gateway_user(
    user_id: str,
    payload: AcquireGatewayUserRequest,
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    try:
        context = service.acquire_user(
            user_id=user_id,
            client_label=payload.client_label,
        )
    except UserLeaseOccupiedError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "user_lease_occupied",
                "client_label": error.summary.client_label,
                "expires_at": error.summary.expires_at,
            },
        ) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    _release_replaced_user_access(request, service, context)
    _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/users/{user_id}/takeover",
    response_model=APIResponse[GatewayUserAccessDTO],
)
async def takeover_gateway_user(
    user_id: str,
    payload: AcquireGatewayUserRequest,
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    try:
        context = service.acquire_user(
            user_id=user_id,
            client_label=payload.client_label,
            takeover=True,
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    _release_replaced_user_access(request, service, context)
    _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service, takeover=True),
        request_id=request_id,
    )


@router.post("/api/gateway/users/guest", response_model=APIResponse[GatewayUserAccessDTO])
async def acquire_gateway_guest(
    payload: CreateGatewayGuestRequest,
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = service.acquire_guest(tracking=payload.tracking)
    _release_replaced_user_access(request, service, context)
    _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@router.get("/api/gateway/users/current", response_model=APIResponse[GatewayUserAccessDTO])
async def current_gateway_user(
    request: Request,
    response: Response,
    x_local_token: str | None = Header(default=None),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    # 首次页面加载会在拿到本地凭据前探测当前用户；本机没有 cookie 时应直接
    # 建立游客态，而不是让浏览器产生一个无意义的 401 资源错误。若调用方
    # 主动带 token，仍必须经过同一凭据校验，不能借此放宽已认证请求。
    if x_local_token is not None and x_local_token != get_gateway_local_token():
        raise HTTPException(status_code=401, detail="invalid local token")
    context = service.resolve_cookie(request.cookies.get(USER_ACCESS_COOKIE_NAME))
    if context is None:
        # 首次页面加载完全没有 cookie 时按首载契约建立游客态，避免业务
        # 初始化先看到一次无意义的 401；cookie 存在但已失效（被接管/释放/
        # 过期）时必须返回 401，不得静默重建游客态——否则并发到达的陈旧
        # 请求会让游客 Set-Cookie 覆盖同客户端刚完成的用户切换（takeover
        # 竞态，gateway_user_view 双浏览器集成实证：接管 POST 200 后轮询
        # /users/current 的陈旧 cookie 把刚写入的用户 cookie 覆盖回游客）。
        if USER_ACCESS_COOKIE_NAME in request.cookies:
            raise HTTPException(status_code=401, detail="user_session_required")
        context = service.acquire_guest()
        _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/users/current/heartbeat",
    response_model=APIResponse[GatewayUserAccessDTO],
)
async def heartbeat_gateway_user(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = _current_user_access(request, service)
    try:
        context = service.heartbeat(context)
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@router.delete("/api/gateway/users/current")
async def release_gateway_user(
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = _current_user_access(request, service)
    service.release(context)
    response.delete_cookie(key=USER_ACCESS_COOKIE_NAME, path="/")
    return APIResponse(data={"released": True}, request_id=request_id)


@router.get("/api/gateway/users/current/view-state")
async def get_gateway_user_view_state(
    request: Request,
    workspace_id: str = Query(min_length=1, max_length=256),
    session_id: str = Query(min_length=1, max_length=256),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    store: UserViewStateStore = Depends(get_user_view_state_store),
):
    context = _current_user_access(request, service)
    try:
        record = store.get(
            context=context,
            workspace_id=workspace_id,
            session_id=session_id,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return APIResponse(
        data=_view_state_dto(record) if record is not None else None,
        request_id=request_id,
    )


@router.get("/api/gateway/users/current/view-state/latest")
async def get_latest_gateway_user_view_state(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    store: UserViewStateStore = Depends(get_user_view_state_store),
):
    context = _current_user_access(request, service)
    try:
        record = store.get_latest(context=context)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return APIResponse(
        data=_view_state_dto(record) if record is not None else None,
        request_id=request_id,
    )


@router.put("/api/gateway/users/current/view-state")
async def put_gateway_user_view_state(
    payload: GatewayUserViewStateUpdateRequest,
    request: Request,
    workspace_id: str = Query(min_length=1, max_length=256),
    session_id: str = Query(min_length=1, max_length=256),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    store: UserViewStateStore = Depends(get_user_view_state_store),
):
    context = _current_user_access(request, service)
    try:
        record = store.put(
            context=context,
            workspace_id=workspace_id,
            session_id=session_id,
            turn_anchor=payload.turn_anchor,
            scroll_offset=payload.scroll_offset,
            follow_latest=payload.follow_latest,
            projection_version=payload.projection_version,
            tool_details_expanded=payload.tool_details_expanded,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return APIResponse(data=_view_state_dto(record), request_id=request_id)
