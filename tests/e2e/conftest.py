from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Generator, Sequence
from hashlib import sha1
from pathlib import Path

import httpx
import pytest
import debugpy

from tests.e2e.ports import e2e_port_block_for_file
from tests.e2e.processes import close_backend_process, start_backend_process


@pytest.fixture(scope="session", autouse=True)
def e2e_session_marker() -> str:
    """用于区分不同 pytest 会话，确保每次运行都从零开始。"""
    return uuid.uuid4().hex


@pytest.fixture(scope="session", autouse=True)
def is_debug() -> bool:
    return os.getenv("BOXTEAM_E2E_BACKEND_DEBUGPY") == "1"


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest, e2e_session_marker: str) -> str:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests" / "e2e"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    workspace_root = project_root / "out" / "tests" / "e2e" / relative_test_path

    default_workspace_root = project_root / "asset" / "default_test_workspace"
    lock_file = workspace_root / ".e2e_session_lock"

    same_session = False
    if lock_file.exists():
        try:
            same_session = lock_file.read_text(encoding="utf-8").strip() == e2e_session_marker
        except Exception:
            same_session = False

    if workspace_root.exists() and same_session:
        # 同一次 pytest 会话内，保留 .boxteam，只清理工作区其余内容
        boxteam_dir = workspace_root / ".boxteam"
        for item in workspace_root.iterdir():
            if item.resolve() == boxteam_dir.resolve() or item.resolve() == lock_file.resolve():
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    else:
        # 新的 pytest 会话，清理整个 workspace（包括旧的 .boxteam）
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

    lock_file.write_text(e2e_session_marker, encoding="utf-8")
    return str(workspace_root)


@pytest.fixture(scope="module")
def e2e_config_path() -> str:
    return str(Path.cwd().resolve() / "configs" / "tests" / "default.jsonc")


@pytest.fixture(autouse=True)
def setup_e2e_test_config(e2e_config_path: str):
    from app.services.infrastructure.config_service import set_config_path

    set_config_path(e2e_config_path)
    yield
    set_config_path(None)


@pytest.fixture(scope="module")
def e2e_backend_port(request: pytest.FixtureRequest) -> int:
    return e2e_port_block_for_file(Path(request.node.fspath)).backend_port


@pytest.fixture(scope="module")
def e2e_backend_process(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
    is_debug: bool,
) -> Generator[subprocess.Popen[str], None, None]:
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


def pytest_collection_modifyitems(items: Sequence[pytest.Item]) -> None:
    """将 e2e 测试按文件路径分组，确保同一文件内测试串行执行，并配合文件级独立后端进程。"""

    for item in items:
        path_key = item.path.as_posix() if hasattr(item, "path") else item.nodeid
        group_suffix = sha1(path_key.encode("utf-8")).hexdigest()[:8]
        group_name = f"e2e_file_{group_suffix}"
        item.add_marker(pytest.mark.xdist_group(name=group_name))
