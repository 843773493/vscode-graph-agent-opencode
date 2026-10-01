"""Gateway Web UI 设置、主题与 UI 资源路由。"""

from __future__ import annotations

from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse

from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.config import GatewayConfig
from app.gateway.control.user_access import (
    UserAccessContext,
    UserAccessService,
)
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.routes._shared import (
    _current_user_access,
    _gateway_root,
    get_user_access_service,
    get_user_profile_store,
)
from app.gateway.theme import (
    MAX_UI_ASSET_BYTES,
    delete_ui_asset,
    import_ui_asset,
    list_ui_assets,
    load_validated_theme_config,
    referenced_asset_ids,
    resolve_settings_theme,
    resolve_theme,
    resolve_ui_asset,
    synchronize_theme_asset_references,
    theme_catalog,
)
from app.gateway.ui_settings import merge_web_ui_settings_values
from app.schemas.gateway import (
    GatewayThemeCatalogDTO,
    GatewayUIAssetDTO,
    GatewayUIAssetListDTO,
    WebUISettingsDTO,
    WebUISettingsUpdateDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


def _read_current_ui_settings(
    request: Request,
    *,
    access_service: UserAccessService,
    profiles: UserProfileStore,
) -> tuple[UserAccessContext, WebUISettingsDTO]:
    context = _current_user_access(request, access_service)
    if context.kind == "guest":
        return context, WebUISettingsDTO()
    if context.user_id is None:
        raise RuntimeError("普通用户访问上下文缺少 user_id")
    return context, profiles.read_ui_settings(user_id=context.user_id)


def _theme_asset_root(
    context: UserAccessContext,
    profiles: UserProfileStore,
) -> Path:
    if context.kind == "guest":
        # 游客不创建 profile；游客视图只能使用内置主题或网络背景。
        return _gateway_root() / "guest-theme-assets"
    if context.user_id is None:
        raise RuntimeError("普通用户访问上下文缺少 user_id")
    return profiles.theme_assets_path(user_id=context.user_id)


def _theme_config_for_access(
    context: UserAccessContext,
    *,
    profiles: UserProfileStore,
    base_config: GatewayConfig | None,
    gateway_root: Path,
) -> GatewayConfig:
    config = base_config or load_validated_theme_config(gateway_root=gateway_root)
    if context.kind == "guest":
        return profiles.guest_theme_config(config)
    if context.user_id is None:
        raise RuntimeError("普通用户访问上下文缺少 user_id")
    return profiles.theme_config(user_id=context.user_id, base_config=config)


@router.get("/api/gateway/ui-settings", response_model=APIResponse[WebUISettingsDTO])
async def get_web_ui_settings(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        theme_asset_root = _theme_asset_root(context, profiles)
        theme_config = _theme_config_for_access(
            context,
            profiles=profiles,
            base_config=getattr(request.app.state, "gateway_config", None),
            gateway_root=theme_asset_root,
        )
        settings = resolve_settings_theme(
            user_settings,
            config=load_validated_theme_config(
                gateway_root=theme_asset_root,
                config=theme_config,
            ),
            gateway_root=theme_asset_root,
        )
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=settings, request_id=request_id)


@router.put("/api/gateway/ui-settings", response_model=APIResponse[WebUISettingsDTO])
async def update_web_ui_settings(
    payload: WebUISettingsUpdateDTO,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, current_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        theme_asset_root = _theme_asset_root(context, profiles)
        config = load_validated_theme_config(
            gateway_root=theme_asset_root,
            config=_theme_config_for_access(
                context,
                profiles=profiles,
                base_config=getattr(request.app.state, "gateway_config", None),
                gateway_root=theme_asset_root,
            ),
        )
        if payload.theme is not None:
            theme_id = (
                payload.theme.theme_id
                or current_settings.theme.theme_id
                or config.default_theme_id
            )
            background = (
                payload.theme.background
                if "background" in payload.theme.model_fields_set
                else current_settings.theme.background
            )
            resolve_theme(
                theme_id,
                config=config,
                gateway_root=theme_asset_root,
                background_override=background,
            )
        updated = merge_web_ui_settings_values(current_settings, payload)
        if context.kind == "user":
            if context.user_id is None:
                raise RuntimeError("普通用户访问上下文缺少 user_id")
            profiles.write_ui_settings(user_id=context.user_id, settings=updated)
        resolved = resolve_settings_theme(
            updated,
            config=config,
            gateway_root=theme_asset_root,
        )
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=resolved, request_id=request_id)


@router.get("/api/gateway/themes", response_model=APIResponse[GatewayThemeCatalogDTO])
async def get_gateway_themes(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        theme_asset_root = _theme_asset_root(context, profiles)
        theme_config = _theme_config_for_access(
            context,
            profiles=profiles,
            base_config=getattr(request.app.state, "gateway_config", None),
            gateway_root=theme_asset_root,
        )
        catalog = theme_catalog(
            user_settings,
            config=load_validated_theme_config(
                gateway_root=theme_asset_root,
                config=theme_config,
            ),
            gateway_root=theme_asset_root,
        )
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=catalog, request_id=request_id)


@router.get("/api/gateway/ui-assets", response_model=APIResponse[GatewayUIAssetListDTO])
async def get_gateway_ui_assets(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        if context.kind == "guest":
            assets = []
        else:
            theme_asset_root = _theme_asset_root(context, profiles)
            theme_config = _theme_config_for_access(
                context,
                profiles=profiles,
                base_config=getattr(request.app.state, "gateway_config", None),
                gateway_root=theme_asset_root,
            )
            synchronize_theme_asset_references(
                load_validated_theme_config(
                    gateway_root=theme_asset_root,
                    config=theme_config,
                ),
                user_settings,
                gateway_root=theme_asset_root,
            )
            assets = list_ui_assets(theme_asset_root)
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayUIAssetListDTO(items=assets),
        request_id=request_id,
    )


@router.post("/api/gateway/ui-assets", response_model=APIResponse[GatewayUIAssetDTO])
async def upload_gateway_ui_asset(
    request: Request,
    file: UploadFile = File(),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    context = _current_user_access(request, access_service)
    if context.kind == "guest":
        raise HTTPException(status_code=403, detail="游客不能上传主题资源")
    content = await file.read(MAX_UI_ASSET_BYTES + 1)
    try:
        asset = import_ui_asset(
            content,
            original_filename=file.filename or "background",
            gateway_root=_theme_asset_root(context, profiles),
            declared_content_type=file.content_type,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=asset, request_id=request_id)


@router.get("/api/gateway/ui-assets/{asset_id}", response_class=FileResponse)
async def get_gateway_ui_asset(
    asset_id: str,
    request: Request,
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    context = _current_user_access(request, access_service)
    try:
        path, asset = resolve_ui_asset(
            asset_id,
            gateway_root=_theme_asset_root(context, profiles),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(
        path,
        media_type=asset.content_type,
        filename=asset.original_filename,
        headers={
            "ETag": f'"{asset.sha256}"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
        content_disposition_type="inline",
    )


@router.delete(
    "/api/gateway/ui-assets/{asset_id}",
    response_model=APIResponse[GatewayUIAssetListDTO],
)
async def remove_gateway_ui_asset(
    asset_id: str,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        if context.kind == "guest":
            raise HTTPException(status_code=403, detail="游客不能删除主题资源")
        theme_asset_root = _theme_asset_root(context, profiles)
        theme_config = _theme_config_for_access(
            context,
            profiles=profiles,
            base_config=getattr(request.app.state, "gateway_config", None),
            gateway_root=theme_asset_root,
        )
        references = referenced_asset_ids(
            load_validated_theme_config(
                gateway_root=theme_asset_root,
                config=theme_config,
            ),
            user_settings,
            gateway_root=theme_asset_root,
        ).get(asset_id, [])
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if references:
        raise HTTPException(
            status_code=409,
            detail=f"背景资源正在被主题引用，不能删除: {', '.join(references)}",
        )
    try:
        delete_ui_asset(asset_id, gateway_root=theme_asset_root)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=GatewayUIAssetListDTO(items=list_ui_assets(theme_asset_root)),
        request_id=request_id,
    )
