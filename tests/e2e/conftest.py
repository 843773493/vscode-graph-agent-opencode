from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Generator, Sequence
from hashlib import sha1
from pathlib import Path

import httpx
import pytest
import debugpy

from tests.e2e.ports import e2e_port_block_for_file
from tests.e2e.processes import close_backend_process, start_backend_process


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keep-docker-runtime",
        action="store_true",
        default=False,
        help="仅运行一个 Docker E2E 时保留该测试启动的宿主机和容器内进程",
    )


@pytest.fixture(scope="session", autouse=True)
def is_debug() -> bool:
    return os.getenv("BOXTEAM_E2E_BACKEND_DEBUGPY") == "1"


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests" / "e2e"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    workspace_root = (
        project_root / "out" / "tests" / "temp" / "e2e" / relative_test_path / "workspace"
    )

    default_workspace_root = project_root / "asset" / "default_test_workspace"
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)

    for item in default_workspace_root.iterdir():
        target = workspace_root / item.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    return str(workspace_root)


@pytest.fixture(scope="module")
def e2e_config_path() -> str:
    return str(Path.cwd().resolve() / "configs" / "tests" / "default.jsonc")


@pytest.fixture(scope="module", autouse=True)
def e2e_workspace_config_path(
    e2e_workspace_root_path: str,
    e2e_config_path: str,
) -> str:
    source_path = Path(e2e_config_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"E2E 配置不存在: {source_path}")
    target_path = Path(e2e_workspace_root_path) / ".boxteam" / "boxteam.jsonc"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
    shutil.copy2(
        Path.cwd().resolve() / "configs" / "config.jsonc",
        target_path.parent / "config.schema.jsonc",
    )
    return str(target_path)


@pytest.fixture(scope="module")
def e2e_backend_port(request: pytest.FixtureRequest) -> int:
    return e2e_port_block_for_file(Path(request.node.fspath)).backend_port


@pytest.fixture(scope="module")
def e2e_backend_process(
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    e2e_backend_port: int,
    is_debug: bool,
) -> Generator[subprocess.Popen[str], None, None]:
    if not Path(e2e_workspace_config_path).is_file():
        raise FileNotFoundError(f"E2E 工作区配置不存在: {e2e_workspace_config_path}")
    debugpy_port = int(os.getenv("BOXTEAM_E2E_BACKEND_DEBUGPY_PORT")) if is_debug else None
    handle = start_backend_process(
        workspace_root=e2e_workspace_root_path,
        port=e2e_backend_port,
        log_name="e2e-backend",
        debugpy_port=debugpy_port,
    )
    if debugpy_port is not None:
        print(
            f"启动 e2e 后端进程: port={e2e_backend_port}, debugpy_port={debugpy_port}, "
            f"workspace={e2e_workspace_root_path}, pid={handle.process.pid}"
        )
    else:
        print(
            f"启动 e2e 后端进程: port={e2e_backend_port}, "
            f"workspace={e2e_workspace_root_path}, pid={handle.process.pid}"
        )

    try:
        yield handle.process
    finally:
        close_backend_process(handle)


@pytest.fixture(scope="module")
def client_base_url(e2e_backend_port: int) -> str:
    return f"http://127.0.0.1:{e2e_backend_port}"


@pytest.fixture
async def client(
    e2e_backend_process: subprocess.Popen[str],
    client_base_url: str,
    is_debug: bool
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=client_base_url,
        timeout=30 if not is_debug else None,
        headers={"X-Local-Token": "local-dev-token"},
    ) as client:
        yield client


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Sequence[pytest.Item],
) -> None:
    """将 e2e 测试按文件路径分组，确保同一文件内测试串行执行，并配合文件级独立后端进程。"""

    if config.getoption("--keep-docker-runtime") and len(items) != 1:
        raise pytest.UsageError(
            "--keep-docker-runtime 只能配合一个已收集的测试使用，避免遗留大量端口进程"
        )

    for item in items:
        path_key = item.path.as_posix() if hasattr(item, "path") else item.nodeid
        group_suffix = sha1(path_key.encode("utf-8")).hexdigest()[:8]
        group_name = f"e2e_file_{group_suffix}"
        item.add_marker(pytest.mark.xdist_group(name=group_name))
