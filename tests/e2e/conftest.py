from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import httpx
import pytest

from app.main import app

E2E_MAX_PARALLEL_GROUPS = 5


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
async def app_container() -> AsyncIterator[object]:
    async with app.router.lifespan_context(app):
        yield app.state.container


@pytest.fixture
def background_task_registry(app_container):
    return app_container.background_task_registry


@pytest.fixture
def config_service(app_container):
    return app_container.config_service


@pytest.fixture
def background_message_bus(app_container):
    return app_container.background_message_bus


@pytest.fixture
def job_event_bus(app_container):
    return app_container.job_event_bus


@pytest.fixture
def job_service(app_container):
    return app_container.job_service


@pytest.fixture
def message_service(app_container):
    return app_container.message_service


@pytest.fixture
def session_service(app_container):
    return app_container.session_service


@pytest.fixture
def agent_execution_service(app_container):
    return app_container.agent_execution_service


@pytest.fixture(autouse=True)
def setup_e2e_test_config() -> Generator[None, None, None]:
    from app.services.config_service import set_config_path
    from app.core.path_utils import get_workspace_root

    config_path = Path(__file__).resolve().parents[2] / "configs" / "tests" / "default.jsonc"
    set_config_path(str(config_path))

    # 每次 e2e 运行前清理工作区中的 .boxteam，避免旧 session/trace 数据污染测试结果。
    workspace_root = get_workspace_root()
    boxteam_dir = workspace_root / ".boxteam"
    if boxteam_dir.exists():
        shutil.rmtree(boxteam_dir)

    yield
    set_config_path(None)


def pytest_collection_modifyitems(items: Sequence[pytest.Item]) -> None:
    """将 e2e 测试按最多 5 个并发分组，供 xdist 调度使用。"""

    for index, item in enumerate(items):
        group_name = f"e2e_group_{index % E2E_MAX_PARALLEL_GROUPS}"
        item.add_marker(pytest.mark.xdist_group(name=group_name))


