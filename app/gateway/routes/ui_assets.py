"""Gateway Web UI 图片资源路由。"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse

from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_content_access,
    verify_gateway_token,
)
from app.gateway.routes._shared import (
    _gateway_root,
)
from app.gateway.theme import (
    MAX_UI_ASSET_BYTES,
    delete_ui_asset,
    import_ui_asset,
    list_ui_assets,
    resolve_ui_asset,
)
from app.schemas.gateway.ui_settings import (
    GatewayUIAssetDTO,
    GatewayUIAssetListDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


@router.get("/api/gateway/ui-assets", response_model=APIResponse[GatewayUIAssetListDTO])
async def get_gateway_ui_assets(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        assets = list_ui_assets(_gateway_root())
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayUIAssetListDTO(items=assets),
        request_id=request_id,
    )


@router.post("/api/gateway/ui-assets", response_model=APIResponse[GatewayUIAssetDTO])
async def upload_gateway_ui_asset(
    file: UploadFile = File(),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    content = await file.read(MAX_UI_ASSET_BYTES + 1)
    try:
        asset = import_ui_asset(
            content,
            original_filename=file.filename or "background",
            gateway_root=_gateway_root(),
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
    auth: GatewayAuthContext = Depends(verify_gateway_content_access),
):
    if auth.kind != "local":
        raise HTTPException(status_code=403, detail="Gateway 主题图片只允许本机凭据访问")
    try:
        path, asset = resolve_ui_asset(
            asset_id,
            gateway_root=_gateway_root(),
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
            "Cache-Control": "private, max-age=31536000, immutable",
            "Vary": "Cookie",
        },
        content_disposition_type="inline",
    )


@router.delete(
    "/api/gateway/ui-assets/{asset_id}",
    response_model=APIResponse[GatewayUIAssetListDTO],
)
async def remove_gateway_ui_asset(
    asset_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    theme_asset_root = _gateway_root()
    try:
        delete_ui_asset(asset_id, gateway_root=theme_asset_root)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=GatewayUIAssetListDTO(items=list_ui_assets(theme_asset_root)),
        request_id=request_id,
    )
