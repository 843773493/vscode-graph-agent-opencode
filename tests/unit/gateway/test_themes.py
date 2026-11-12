from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.gateway.auth import (
    GatewayAuthContext,
    get_gateway_local_token,
    verify_gateway_content_access,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.main import app
from app.gateway.theme import import_ui_asset, resolve_ui_asset


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (3, 2), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def test_theme_image_does_not_expand_access_to_federation() -> None:
    from fastapi import FastAPI

    from app.gateway.routes.ui_assets import router

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[verify_gateway_content_access] = lambda: GatewayAuthContext(kind="federation", peer_gateway_id="peer")
    response = TestClient(application).get("/api/gateway/ui-assets/example")
    assert response.status_code == 403


def test_asset_is_deduplicated_and_resolved_inside_store(tmp_path):
    content = _png_bytes()
    first = import_ui_asset(
        content,
        original_filename="first.png",
        gateway_root=tmp_path,
    )
    second = import_ui_asset(
        content,
        original_filename="second.png",
        gateway_root=tmp_path,
    )

    assert first.asset_id == second.asset_id
    path, resolved = resolve_ui_asset(first.asset_id, gateway_root=tmp_path)
    assert path.read_bytes() == content
    assert resolved.url == f"/api/gateway/ui-assets/{first.asset_id}"


def test_asset_rejects_mismatched_extension_and_path_like_id(tmp_path):
    with pytest.raises(ValueError, match="扩展名与内容格式不匹配"):
        import_ui_asset(
            _png_bytes(),
            original_filename="background.jpg",
            gateway_root=tmp_path,
        )
    with pytest.raises(KeyError, match="UI 资源不存在"):
        resolve_ui_asset("../../gateway.jsonc", gateway_root=tmp_path)
    with pytest.raises(ValueError, match="MIME type 与实际内容不匹配"):
        import_ui_asset(
            _png_bytes(),
            original_filename="background.png",
            declared_content_type="image/jpeg",
            gateway_root=tmp_path,
        )


def test_gateway_assets_serve_and_delete_without_theme_state(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
):
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    state = GatewayStateStore(path=gateway_root / "gateway.sqlite")
    request.addfinalizer(state.close)
    client = TestClient(app)
    headers = {"X-Local-Token": get_gateway_local_token()}

    assert client.get("/api/gateway/themes", headers=headers).status_code == 404
    settings = client.get("/api/gateway/ui-settings", headers=headers)
    assert settings.status_code == 404
    assert client.put("/api/gateway/ui-settings", headers=headers, json={"theme": {}}).status_code == 404

    upload_response = client.post(
        "/api/gateway/ui-assets",
        headers=headers,
        files={"file": ("background.png", _png_bytes(), "image/png")},
    )
    assert upload_response.status_code == 200
    asset = upload_response.json()["data"]

    asset_list_response = client.get("/api/gateway/ui-assets", headers=headers)
    assert asset_list_response.status_code == 200
    listed_asset = asset_list_response.json()["data"]["items"][0]
    assert listed_asset["asset_id"] == asset["asset_id"]
    assert "referenced_theme_ids" not in listed_asset

    image_response = client.get(asset["url"], headers=headers)
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"
    assert image_response.headers["etag"] == f'"{asset["sha256"]}"'
    assert image_response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert image_response.headers["vary"] == "Cookie"

    deleted = client.delete(
        f"/api/gateway/ui-assets/{asset['asset_id']}",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["items"] == []
    assert client.get(asset["url"], headers=headers).status_code == 404
