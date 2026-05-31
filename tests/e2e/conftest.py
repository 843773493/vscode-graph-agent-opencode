from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.main import app


@pytest.fixture
async def client(workspace_root_path: str) -> AsyncIterator[httpx.AsyncClient]:
    print(f"使用测试工作区: {workspace_root_path}")
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            timeout=30,
            headers={"X-Local-Token": "local-dev-token"},
        ) as client:
            yield client


@pytest.fixture
async def container() -> AsyncIterator[object]:
    async with app.router.lifespan_context(app):
        yield app.state.container


@pytest.fixture(autouse=True)
def setup_e2e_test_config() -> None:
    from app.services.config_service import set_config_path

    config_path = Path(__file__).resolve().parents[2] / "configs" / "tests" / "default.jsonc"
    set_config_path(str(config_path))
    yield
    set_config_path(None)


@pytest.fixture(autouse=True)
def ensure_real_model_skip_marker() -> None:
    if not os.environ.get("OPENCODE_ZEN_API_KEY"):
        pytest.skip("缺少 OPENCODE_ZEN_API_KEY，跳过真实模型验证")
