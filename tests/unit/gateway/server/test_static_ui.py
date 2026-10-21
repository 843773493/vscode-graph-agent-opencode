from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.server.static_ui import install_static_web_ui


def write_assets(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<main>BoxTeam UI</main>", encoding="utf-8")
    (root / "assets" / "main.js").write_text(
        "globalThis.boxteam=true;",
        encoding="utf-8",
    )


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
