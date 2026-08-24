from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.path_utils import get_user_gateway_config_path
from app.gateway.auth import get_gateway_local_token
from app.gateway.config import ConfiguredTheme, GatewayConfig
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import UserAccessService, USER_ACCESS_COOKIE_NAME
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.main import app
from app.schemas.gateway import GatewayThemeBackgroundDTO, WebUISettingsDTO
from app.gateway.theme import (
    import_ui_asset,
    load_validated_theme_config,
    resolve_settings_theme,
    resolve_theme,
    resolve_ui_asset,
    theme_catalog,
)
from app.gateway.theme.defaults import DEFAULT_THEME_BACKGROUND_OVERLAY


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (3, 2), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def test_builtin_themes_are_complete_and_default_to_warm(tmp_path):
    config = GatewayConfig()
    catalog = theme_catalog(WebUISettingsDTO(), config=config, gateway_root=tmp_path)

    assert catalog.current_theme_id == "warm"
    assert [item.id for item in catalog.items] == ["warm", "green", "blue"]
    token_sets = [
        set(resolve_theme(theme_id, config=config, gateway_root=tmp_path).tokens)
        for theme_id in ("warm", "green", "blue")
    ]
    assert len(token_sets[0]) >= 80
    assert token_sets[0] == token_sets[1] == token_sets[2]
    assert (
        resolve_theme("green", config=config, gateway_root=tmp_path).tokens[
            "--bt-accent"
        ]
        == "#287a55"
    )
    assert (
        resolve_theme("warm", config=config, gateway_root=tmp_path).tokens[
            "--bt-accent"
        ]
        == "#b96f32"
    )
    assert {
        "--bt-canvas-background",
        "--bt-chrome-surface",
        "--bt-workspace-surface",
        "--bt-panel-surface",
        "--bt-floating-surface",
        "--bt-critical-surface",
        "--bt-toolbar-background",
        "--bt-workspace-header-background",
        "--bt-bottom-panel-background",
        "--bt-bottom-panel-header-background",
        "--bt-bottom-panel-toolbar-background",
        "--bt-bottom-panel-list-background",
        "--bt-bottom-panel-viewer-background",
        "--bt-runtime-preview-background",
        "--bt-runtime-preview-header-background",
        "--bt-runtime-preview-border",
        "--bt-status-bar-background",
        "--bt-status-bar-border",
        "--bt-status-bar-foreground",
        "--bt-surface-backdrop-filter",
        "--bt-chrome-backdrop-filter",
        "--bt-workspace-backdrop-filter",
    } <= token_sets[0]


def test_custom_theme_inherits_and_overrides_builtin(tmp_path):
    config = GatewayConfig(
        default_theme_id="forest",
        custom_themes=(
            ConfiguredTheme(
                id="forest",
                label="森林",
                extends="green",
                color_scheme="light",
                tokens={"--bt-accent": "#123456"},
            ),
        ),
    )

    resolved = resolve_settings_theme(
        WebUISettingsDTO(),
        config=config,
        gateway_root=tmp_path,
    )

    assert resolved.theme.theme_id == "forest"
    assert resolved.theme.resolved_theme is not None
    assert resolved.theme.resolved_theme.tokens["--bt-accent"] == "#123456"


def test_custom_theme_can_override_new_workbench_region_tokens(tmp_path):
    config = GatewayConfig(
        custom_themes=(
            ConfiguredTheme(
                id="compact-dark",
                label="紧凑深色",
                extends="blue",
                color_scheme="dark",
                tokens={
                    "--bt-bottom-panel-background": "#10151d",
                    "--bt-status-bar-background": "#183b30",
                },
            ),
        )
    )

    resolved = resolve_theme("compact-dark", config=config, gateway_root=tmp_path)

    assert resolved.tokens["--bt-bottom-panel-background"] == "#10151d"
    assert resolved.tokens["--bt-status-bar-background"] == "#183b30"


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        ({"--unknown": "red"}, "未知主题 token"),
        ({"--bt-accent": ""}, "不允许为空"),
        ({"--bt-accent": "url(https://example.com/x)"}, "不允许的 CSS"),
        ({"--bt-accent": "16px"}, "颜色 token 值无效"),
        ({"--bt-radius-large": "red"}, "尺寸 token 值无效"),
        ({"--bt-background-repeat": "spin"}, "重复 token 值无效"),
        ({"--bt-background-image": "linear-gradient(red, blue)"}, "只能为 none"),
    ],
)
def test_invalid_custom_token_is_rejected(tmp_path, tokens, message):
    config = GatewayConfig(
        custom_themes=(ConfiguredTheme("invalid", "无效", "warm", "light", tokens),)
    )
    with pytest.raises(ValueError, match=message):
        resolve_theme("invalid", config=config, gateway_root=tmp_path)


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


def test_gateway_asset_background_is_returned_as_same_origin_url(tmp_path):
    asset = import_ui_asset(
        _png_bytes(),
        original_filename="background.png",
        gateway_root=tmp_path,
    )
    background = GatewayThemeBackgroundDTO(
        type="gateway_asset",
        asset_id=asset.asset_id,
    )

    resolved = resolve_theme(
        "blue",
        config=GatewayConfig(),
        gateway_root=tmp_path,
        background_override=background,
    )

    assert resolved.background_image_url == asset.url
    assert resolved.tokens["--bt-background-size"] == "cover"
    assert (
        resolved.tokens["--bt-background-overlay"] == DEFAULT_THEME_BACKGROUND_OVERLAY
    )
    assert resolved.color_scheme == "dark"
    assert resolved.tokens["--bt-page-background"] == "#111318"
    assert resolved.tokens["--bt-text-primary"] == "#edf0f1"
    assert resolved.tokens["--bt-accent"] == "#2f64b4"
    assert resolved.tokens["--bt-link-foreground"] == "var(--bt-accent)"
    assert "var(--bt-accent)" in resolved.tokens["--bt-focus-border"]


def test_background_can_keep_selected_theme_palette(tmp_path):
    background = GatewayThemeBackgroundDTO(
        type="remote",
        url="https://example.com/background.png",
        appearance="theme",
    )

    resolved = resolve_theme(
        "warm",
        config=GatewayConfig(),
        gateway_root=tmp_path,
        background_override=background,
    )

    assert resolved.color_scheme == "light"
    assert resolved.tokens["--bt-page-background"] == "#f2ecd9"
    assert resolved.tokens["--bt-text-primary"] == "#39352b"


def test_background_overlay_can_be_explicitly_disabled(tmp_path):
    background = GatewayThemeBackgroundDTO(
        type="remote",
        url="https://example.com/background.png",
        overlay="none",
    )

    resolved = resolve_theme(
        "warm",
        config=GatewayConfig(),
        gateway_root=tmp_path,
        background_override=background,
    )

    assert resolved.tokens["--bt-background-overlay"] == "none"


def test_configured_background_without_overlay_uses_readability_default(tmp_path):
    config = GatewayConfig(
        custom_themes=(
            ConfiguredTheme(
                id="network",
                label="网络背景",
                extends="blue",
                color_scheme="light",
                tokens={},
                background={
                    "type": "remote",
                    "url": "https://example.com/background.png",
                },
            ),
        )
    )

    resolved = resolve_theme("network", config=config, gateway_root=tmp_path)

    assert (
        resolved.tokens["--bt-background-overlay"] == DEFAULT_THEME_BACKGROUND_OVERLAY
    )


def test_remote_background_rejects_non_http_scheme(tmp_path):
    background = GatewayThemeBackgroundDTO(type="remote", url="file:///tmp/a.png")
    with pytest.raises(ValueError, match="http/https"):
        resolve_theme(
            "warm",
            config=GatewayConfig(),
            gateway_root=tmp_path,
            background_override=background,
        )


def test_gateway_theme_api_switches_and_serves_uploaded_background(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
):
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    state = GatewayStateStore(path=gateway_root / "gateway.sqlite")
    request.addfinalizer(state.close)
    access_service = UserAccessService(state=state)
    user = access_service.create_user(display_name="主题测试用户", user_id="theme-user")
    UserProfileStore(gateway_root=gateway_root).ensure_user(
        user_id=user.user_id,
        display_name=user.display_name,
    )
    access = access_service.acquire_user(
        user_id=user.user_id,
        client_label="unit-test",
    )
    monkeypatch.setattr(app.state, "user_access_service", access_service, raising=False)
    monkeypatch.setattr(
        app.state,
        "user_profile_store",
        UserProfileStore(gateway_root=gateway_root),
        raising=False,
    )
    client = TestClient(app)
    client.cookies.set(USER_ACCESS_COOKIE_NAME, access.access_session_id)
    headers = {"X-Local-Token": get_gateway_local_token()}

    catalog_response = client.get("/api/gateway/themes", headers=headers)
    assert catalog_response.status_code == 200
    assert catalog_response.json()["data"]["current_theme_id"] == "warm"

    switch_response = client.put(
        "/api/gateway/ui-settings",
        headers=headers,
        json={"theme": {"theme_id": "blue"}},
    )
    assert switch_response.status_code == 200
    assert switch_response.json()["data"]["theme"]["resolved_theme"]["id"] == "blue"

    upload_response = client.post(
        "/api/gateway/ui-assets",
        headers=headers,
        files={"file": ("background.png", _png_bytes(), "image/png")},
    )
    assert upload_response.status_code == 200
    asset = upload_response.json()["data"]

    background_response = client.put(
        "/api/gateway/ui-settings",
        headers=headers,
        json={
            "theme": {
                "background": {
                    "type": "gateway_asset",
                    "asset_id": asset["asset_id"],
                    "position": "center",
                    "size": "cover",
                    "repeat": "no-repeat",
                    "overlay": "none",
                }
            }
        },
    )
    assert background_response.status_code == 200
    resolved_theme = background_response.json()["data"]["theme"]["resolved_theme"]
    assert resolved_theme["background_image_url"] == asset["url"]

    asset_list_response = client.get("/api/gateway/ui-assets", headers=headers)
    assert asset_list_response.status_code == 200
    listed_asset = asset_list_response.json()["data"]["items"][0]
    assert listed_asset["referenced_theme_ids"] == ["blue"]

    image_response = client.get(asset["url"])
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"
    assert image_response.headers["etag"] == f'"{asset["sha256"]}"'

    blocked_delete = client.delete(
        f"/api/gateway/ui-assets/{asset['asset_id']}",
        headers=headers,
    )
    assert blocked_delete.status_code == 409
    assert "正在被主题引用" in blocked_delete.json()["detail"]


def test_gateway_theme_assets_are_isolated_by_user_profile(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
):
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    state = GatewayStateStore(path=gateway_root / "gateway.sqlite")
    request.addfinalizer(state.close)
    access_service = UserAccessService(state=state)
    profiles = UserProfileStore(gateway_root=gateway_root)
    user_a = access_service.create_user(display_name="主题用户 A", user_id="theme-a")
    user_b = access_service.create_user(display_name="主题用户 B", user_id="theme-b")
    profiles.ensure_user(user_id=user_a.user_id, display_name=user_a.display_name)
    profiles.ensure_user(user_id=user_b.user_id, display_name=user_b.display_name)
    access_a = access_service.acquire_user(user_id=user_a.user_id, client_label="A")
    access_b = access_service.acquire_user(user_id=user_b.user_id, client_label="B")
    monkeypatch.setattr(app.state, "user_access_service", access_service, raising=False)
    monkeypatch.setattr(app.state, "user_profile_store", profiles, raising=False)
    client = TestClient(app)
    headers = {"X-Local-Token": get_gateway_local_token()}

    client.cookies.set(USER_ACCESS_COOKIE_NAME, access_a.access_session_id)
    upload_response = client.post(
        "/api/gateway/ui-assets",
        headers=headers,
        files={"file": ("background.png", _png_bytes(), "image/png")},
    )
    assert upload_response.status_code == 200, upload_response.text
    asset_id = upload_response.json()["data"]["asset_id"]
    assert (
        profiles.theme_assets_path(user_id=user_a.user_id) / "ui-assets" / "manifest.json"
    ).is_file()
    assert not (
        profiles.theme_assets_path(user_id=user_b.user_id)
        / "ui-assets"
        / "manifest.json"
    ).exists()

    client.cookies.set(USER_ACCESS_COOKIE_NAME, access_b.access_session_id)
    list_response = client.get("/api/gateway/ui-assets", headers=headers)
    assert list_response.status_code == 200, list_response.text
    assert list_response.json()["data"]["items"] == []
    other_user_response = client.get(
        f"/api/gateway/ui-assets/{asset_id}",
        headers=headers,
    )
    assert other_user_response.status_code == 404


def test_invalid_theme_config_keeps_previous_valid_snapshot(tmp_path):
    config_path = get_user_gateway_config_path()
    original = config_path.read_text(encoding="utf-8")
    first = load_validated_theme_config(gateway_root=tmp_path)
    assert first.default_theme_id == "warm"

    config_path.write_text(
        original.replace(
            '"default_theme_id": "warm"',
            '"default_theme_id": "missing"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="已保留上一份有效主题配置"):
        load_validated_theme_config(gateway_root=tmp_path)

    config_path.write_text(original, encoding="utf-8")
    restored = load_validated_theme_config(gateway_root=tmp_path)
    assert restored.default_theme_id == "warm"
