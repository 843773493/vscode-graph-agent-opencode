import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.server.static_ui import install_static_web_ui


def write_assets(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<main>BoxTeam UI</main>", encoding="utf-8")
    (root / "mcp-oauth-callback.html").write_text("<main>私有回调</main>", encoding="utf-8")
    (root / "assets" / "main.js").write_text(
        "globalThis.boxteam=true;",
        encoding="utf-8",
    )
    (root / "assets" / "font-ABC12345.ttf").write_bytes(b"font-v1")
    (root / "assets" / "lookalike-ABC12345.js").write_text("unlisted", encoding="utf-8")
    (root / "assets" / "worker.js").write_text("worker-v1", encoding="utf-8")
    (root / "immutable-assets.json").write_text(json.dumps(["assets/font-ABC12345.ttf"]), encoding="utf-8")


@pytest.fixture
def static_ui_client(tmp_path: Path) -> Iterator[TestClient]:
    write_assets(tmp_path)
    app = FastAPI()
    install_static_web_ui(app, assets_root=tmp_path)
    with TestClient(app) as client:
        yield client


def test_static_ui_serves_assets_and_spa_fallback(
    static_ui_client: TestClient,
) -> None:
    assert static_ui_client.get("/").text == "<main>BoxTeam UI</main>"
    assert static_ui_client.get("/assets/main.js").text == "globalThis.boxteam=true;"
    assert static_ui_client.get("/sessions/ses_test").text == "<main>BoxTeam UI</main>"


def test_static_ui_normalizes_backslash_mount_paths(
    static_ui_client: TestClient,
) -> None:
    response = static_ui_client.get("/assets%5Cmain.js")

    assert response.text == "globalThis.boxteam=true;"


def test_static_ui_does_not_turn_unknown_api_into_spa(
    static_ui_client: TestClient,
) -> None:
    response = static_ui_client.get("/api/unknown")

    assert response.status_code == 404
    assert "BoxTeam UI" not in response.text


def test_static_ui_requires_index(tmp_path: Path) -> None:
    app = FastAPI()

    try:
        install_static_web_ui(app, assets_root=tmp_path)
    except FileNotFoundError as error:
        assert "index.html" in str(error)
    else:
        raise AssertionError("缺少 index.html 时必须快速失败")


@pytest.mark.parametrize("path", ["/", "/sessions/ses_test", "/assets/main.js", "/assets/worker.js", "/assets/lookalike-ABC12345.js"])
def test_entry_bundle_and_worker_revalidate(static_ui_client: TestClient, path: str) -> None:
    original = static_ui_client.get(path)
    assert original.status_code == 200
    assert original.headers["cache-control"] == "no-cache"
    unchanged = static_ui_client.get(path, headers={"If-None-Match": original.headers["etag"]})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["cache-control"] == "no-cache"


def test_only_build_listed_content_address_is_immutable(static_ui_client: TestClient) -> None:
    response = static_ui_client.get("/assets/font-ABC12345.ttf")
    assert response.content == b"font-v1"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_bundle_upgrade_and_missing_worker_do_not_reuse_spa(static_ui_client: TestClient, tmp_path: Path) -> None:
    old = static_ui_client.get("/assets/worker.js")
    (tmp_path / "assets/worker.js").write_text("worker-version-two", encoding="utf-8")
    updated = static_ui_client.get("/assets/worker.js", headers={"If-None-Match": old.headers["etag"]})
    assert updated.status_code == 200
    assert updated.text == "worker-version-two"
    assert updated.headers["etag"] != old.headers["etag"]
    (tmp_path / "assets/worker.js").unlink()
    missing = static_ui_client.get("/assets/worker.js", headers={"If-None-Match": updated.headers["etag"]})
    assert missing.status_code == 404
    assert "BoxTeam UI" not in missing.text
    assert static_ui_client.get("/out/missing-worker").status_code == 404


def test_invalid_build_cache_manifest_fails(tmp_path: Path) -> None:
    write_assets(tmp_path)
    (tmp_path / "immutable-assets.json").write_text('{"unexpected":"manifest"}', encoding="utf-8")
    with pytest.raises(ValueError, match="缓存清单"):
        install_static_web_ui(FastAPI(), assets_root=tmp_path)


def test_oauth_callback_is_not_cached_and_access_log_omits_private_query(
    static_ui_client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = static_ui_client.get("/mcp-oauth-callback.html?code=private-code&state=private-state")
    assert response.text == "<main>私有回调</main>"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    access_logger = logging.getLogger("uvicorn.access")
    # SDK/uvicorn 的真实集成装具会改全局日志配置；本测试注入独立收集器并完整恢复。
    monkeypatch.setattr(access_logger, "handlers", [caplog.handler])
    monkeypatch.setattr(access_logger, "propagate", False)
    monkeypatch.setattr(access_logger, "disabled", False)
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        for target in ("/mcp-oauth-callback.html?code=private-code&state=private-state", "/api/gateway/workspaces?view=details"):
            logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", target, "1.1", 200)
    assert '"GET /mcp-oauth-callback.html HTTP/1.1" 200' in caplog.text
    assert "private-code" not in caplog.text and "private-state" not in caplog.text
    assert "/api/gateway/workspaces?view=details" in caplog.text
