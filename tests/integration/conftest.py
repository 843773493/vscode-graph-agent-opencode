from __future__ import annotations

import os
import shutil
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

_PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture(scope="session", autouse=True)
def _hermetic_proxy_env():
    """R19 测试内 hermetic 修复：整个 integration 会话清掉本机代理变量。

    integration 用例全部经 127.0.0.1 本地端口访问真实后端子进程，不经
    任何外部网络；而环境的 NO_PROXY 含 IPv6 字面量（``::1``/``[::1]``）
    会让测试进程与后端子进程内的 httpx 客户端在代理解析时直接抛
    ``InvalidURL: Invalid port: ':1]'``——属外部环境噪声混入测试。与
    R18/R19 对 unit 层 gateway 客户端的同类修复一致（任务书许可的清
    ``*_proxy`` 做法）。session 级在最先装配，保证后端子进程
    （``start_backend_process`` 继承 os.environ）同样拿到干净环境；
    会话结束后原样恢复。
    """
    saved = {
        name: os.environ.pop(name)
        for name in _PROXY_ENV_NAMES
        if name in os.environ
    }
    try:
        yield
    finally:
        os.environ.update(saved)


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
    # Gateway 控制面状态（工作区注册表、导航树、会话目录缓存）位于工作区旁的
    # boxteam-home，与工作区一样跨运行持久；这里沿用 gateway 测试的状态隔离
    # 模式，让每个测试文件都从干净的控制面开始，避免上一次运行遗留的工作区
    # 注册项把导航节点集合或跨工作区目录聚合结果多算一份。
    boxteam_home = output_root / "boxteam-home"
    if boxteam_home.exists():
        shutil.rmtree(boxteam_home)
    workspace_root = prepare_default_test_workspace(
        workspace_root=output_root / "workspace",
        template_root=(
            project_root
            / "tests"
            / "fixtures"
            / "workspaces"
            / "default_test_workspace"
        ),
        shared_skill_root=project_root / "resources" / "skills",
    )
    return str(workspace_root)


@pytest.fixture(scope="module")
def integration_config_path() -> str:
    return str(
        Path.cwd().resolve()
        / "configs"
        / "tests"
        / "workspace"
        / "default.jsonc"
    )


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
