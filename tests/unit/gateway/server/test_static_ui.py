from pathlib import Path

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


def test_static_ui_serves_assets_and_spa_fallback(tmp_path: Path) -> None:
    write_assets(tmp_path)
    app = FastAPI()
    install_static_web_ui(app, assets_root=tmp_path)
    client = TestClient(app)

    assert client.get("/").text == "<main>BoxTeam UI</main>"
    assert client.get("/assets/main.js").text == "globalThis.boxteam=true;"
    assert client.get("/sessions/ses_test").text == "<main>BoxTeam UI</main>"


def test_static_ui_does_not_turn_unknown_api_into_spa(tmp_path: Path) -> None:
    write_assets(tmp_path)
    app = FastAPI()
    install_static_web_ui(app, assets_root=tmp_path)

    response = TestClient(app).get("/api/unknown")

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
