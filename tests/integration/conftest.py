from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Generator, Sequence
from hashlib import sha1
from pathlib import Path

import httpx
import pytest

from tests.support.paths import output_root_for_test
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import close_backend_process, start_backend_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(scope="session", autouse=True)
def integration_is_debug() -> bool:
    return os.getenv("BOXTEAM_INTEGRATION_BACKEND_DEBUGPY") == "1"


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    output_root = output_root_for_test(
        Path(request.node.fspath),
        test_layer="integration",
        project_root=project_root,
    )
    workspace_root = prepare_default_test_workspace(
        workspace_root=output_root / "workspace",
        template_root=project_root / "asset" / "default_test_workspace",
        shared_skill_root=project_root / "resources" / "skills",
    )
    return str(workspace_root)


@pytest.fixture(scope="module")
def integration_config_path() -> str:
    return str(Path.cwd().resolve() / "configs" / "tests" / "default.jsonc")


@pytest.fixture(scope="module", autouse=True)
def integration_workspace_config_path(
    integration_workspace_root_path: str,
    integration_config_path: str,
) -> str:
    target_path = install_test_workspace_config(
        workspace_root=Path(integration_workspace_root_path),
        config_path=Path(integration_config_path),
        schema_path=Path.cwd().resolve() / "configs" / "workspace_schema.jsonc",
    )
    return str(target_path)


@pytest.fixture(scope="module")
def integration_backend_port(request: pytest.FixtureRequest) -> int:
    return integration_port_block_for_file(Path(request.node.fspath)).backend_port


@pytest.fixture(scope="module")
def integration_backend_process(
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    integration_backend_port: int,
    integration_is_debug: bool,
) -> Generator[subprocess.Popen[str], None, None]:
    if not Path(integration_workspace_config_path).is_file():
        raise FileNotFoundError(
            f"集成测试工作区配置不存在: {integration_workspace_config_path}"
        )
    debugpy_port = (
        int(os.getenv("BOXTEAM_INTEGRATION_BACKEND_DEBUGPY_PORT"))
        if integration_is_debug
        else None
    )
    handle = start_backend_process(
        workspace_root=integration_workspace_root_path,
        port=integration_backend_port,
        log_name="integration-backend",
        debugpy_port=debugpy_port,
    )
    try:
        yield handle.process
    finally:
        close_backend_process(handle)


@pytest.fixture
async def integration_client(
    integration_backend_process: subprocess.Popen[str],
    integration_backend_port: int,
    integration_is_debug: bool,
) -> AsyncIterator[httpx.AsyncClient]:
    del integration_backend_process
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{integration_backend_port}",
        timeout=None if integration_is_debug else 60,
        headers={"X-Local-Token": "local-dev-token"},
    ) as client:
        yield client


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Sequence[pytest.Item],
) -> None:
    del config
    for item in items:
        item.add_marker(pytest.mark.integration)
        path_key = item.path.as_posix() if hasattr(item, "path") else item.nodeid
        group_suffix = sha1(path_key.encode("utf-8")).hexdigest()[:8]
        item.add_marker(pytest.mark.xdist_group(name=f"integration_file_{group_suffix}"))
