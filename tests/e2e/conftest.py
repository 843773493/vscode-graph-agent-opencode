from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Generator, Sequence
from hashlib import sha1
from pathlib import Path
from urllib.request import urlopen

import httpx
import pytest

E2E_PORT_START = 18600
E2E_PORT_END = 18799
E2E_READY_TIMEOUT_SECONDS = 60


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests" / "e2e"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    workspace_root = Path() / "out" / "tests" / "e2e" / relative_test_path

    default_workspace_root = project_root / "asset" / "default_test_workspace"

    if workspace_root.exists():
        shutil.rmtree(workspace_root)

    shutil.copytree(default_workspace_root, workspace_root)

    boxteam_dir = workspace_root / ".boxteam"
    if boxteam_dir.exists():
        shutil.rmtree(boxteam_dir)

    return str(workspace_root)


@pytest.fixture(scope="module")
def e2e_config_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "configs" / "tests" / "default.jsonc")


@pytest.fixture(autouse=True)
def setup_e2e_test_config(e2e_config_path: str):
    from app.services.config_service import set_config_path

    set_config_path(e2e_config_path)
    yield
    set_config_path(None)


@pytest.fixture(scope="module")
def e2e_backend_port(e2e_workspace_root_path: str) -> int:
    path_key = e2e_workspace_root_path.replace(os.sep, "/")
    group_suffix = int(sha1(path_key.encode("utf-8")).hexdigest()[:4], 16)
    port = E2E_PORT_START + (group_suffix % (E2E_PORT_END - E2E_PORT_START + 1))
    return port


@pytest.fixture(scope="module")
def e2e_backend_process(e2e_workspace_root_path: str, e2e_backend_port: int) -> Generator[subprocess.Popen[str], None, None]:
    _kill_process_on_port(e2e_backend_port)

    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = e2e_workspace_root_path
    env["PYTHONUNBUFFERED"] = "1"
    launch_debug = env.get("LAUNCH_DEBUG")
    if launch_debug:
        e2e_backend_port = 8000
    
    python_executable = _resolve_workspace_python_executable(Path.cwd())

    if launch_debug:
        cmd = [
            str(python_executable),
            "-m",
            "debugpy",
            "--listen",
            "127.0.0.1:8002",
            "--wait-for-client",
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(e2e_backend_port),
            "--log-level",
            "warning",
        ]
        print("检测到 LAUNCH_DEBUG，e2e 后端将通过 debugpy --wait-for-client 启动")
    else:
        cmd = [
            str(python_executable),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(e2e_backend_port),
            "--log-level",
            "warning",
        ]

    process = subprocess.Popen(
        cmd,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"启动 e2e 后端进程: port={e2e_backend_port}, workspace={e2e_workspace_root_path}, pid={process.pid}")

    try:
        _wait_for_backend_ready(e2e_backend_port, process)
        yield process
    finally:
        _terminate_process(process)
        _kill_process_on_port(e2e_backend_port)


@pytest.fixture(scope="module")
def client_base_url(e2e_backend_port: int) -> str:
    return f"http://127.0.0.1:{e2e_backend_port}"


@pytest.fixture
async def client(
    e2e_backend_process: subprocess.Popen[str],
    client_base_url: str,
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=client_base_url,
        timeout=30,
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


def _wait_for_backend_ready(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + E2E_READY_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{port}/api/v1/health"

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"后端进程提前退出，返回码: {process.returncode}\n"
            )
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)

    raise TimeoutError(
        f"后端在 {E2E_READY_TIMEOUT_SECONDS} 秒内未就绪，端口: {port}\n"
    )


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    try:
        process.terminate()
        process.wait(timeout=10)
        return
    except Exception:
        pass

    try:
        process.kill()
        process.wait(timeout=10)
    except Exception:
        pass


def _kill_process_on_port(port: int) -> None:
    if os.name != "nt":
        return

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty OwningProcess"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return

    target_pids: set[int] = set()
    for line in result.stdout.decode("utf-8", errors="ignore").splitlines():
        try:
            target_pids.add(int(line.strip()))
        except ValueError:
            continue

    for pid in target_pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)


def _resolve_workspace_python_executable(project_root: Path) -> Path:
    windows_python = project_root / ".venv" / "Scripts" / "python.exe"
    if windows_python.exists():
        return windows_python

    posix_python = project_root / ".venv" / "bin" / "python"
    if posix_python.exists():
        return posix_python

    raise FileNotFoundError(
        f"未找到工作区虚拟环境 Python，可尝试路径: {windows_python} 或 {posix_python}"
    )
