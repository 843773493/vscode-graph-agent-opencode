from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack
from io import StringIO
from pathlib import Path
from typing import Literal

import psutil
import pytest

from tests.harness.python import terminal_backend
from tests.integration.gateway import (
    test_navigation_actor_scope_proxy as navigation_case,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    GatewayProcess,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.scope import capture_test_processes, register_test_process
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    ensure_local_port_available,
    resolve_workspace_python_executable,
    start_backend_process,
    terminate_process,
    wait_for_http_ok,
)


@pytest.mark.skipif(sys.platform != "win32", reason="验证原生 Windows venv 启动器与服务器子进程")
@pytest.mark.parametrize("force", [False, True])
def test_windows_python_service_stop_releases_actual_server_before_port_reuse(
    force: bool, tmp_path: Path,
) -> None:
    python = resolve_workspace_python_executable(Path.cwd())
    port = integration_port_block_for_file(Path(__file__)).port(90)
    script = tmp_path / "server.py"
    script.write_text(
        "import sys, uvicorn\n"
        "async def app(scope, receive, send):\n"
        "    await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
        "    await send({'type': 'http.response.body', 'body': b'ready'})\n"
        "uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), lifespan='off', log_level='warning')\n",
        encoding="utf-8",
    )
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(python), str(script), str(port)], stdout=log, stderr=log, text=True)
        children: list[psutil.Process] = []
        try:
            wait_for_http_ok(f"http://127.0.0.1:{port}", process, timeout_seconds=10)
            children = psutil.Process(process.pid).children()
            assert children, "本场景必须持有实际服务器子进程"
            terminate_process(process, force=force)
            # 先检查被测关闭的结果，再运行本用例的兜底回收。
            assert process.poll() is not None
            assert not [child.pid for child in children if child.is_running()]
            ensure_local_port_available(port, service_name="重启服务器")
        finally:
            for child in children:
                if child.is_running():
                    child.kill()
                    child.wait(timeout=10)
            terminate_process(process, force=True)


@pytest.fixture
def process_lifetime() -> Iterator[ExitStack]:
    """测试自建子进程独立归属此 fixture，不受被测嵌套 scope 的释放影响。"""
    with ExitStack() as lifetime:
        yield lifetime


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_listener(port: int, process_lifetime: ExitStack) -> subprocess.Popen[str]:
    script = (
        "import socket, sys\n"
        "server = socket.socket()\n"
        "server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "server.bind(('127.0.0.1', int(sys.argv[1])))\n"
        "server.listen()\n"
        "print('ready', flush=True)\n"
        "while True:\n"
        "    connection, _ = server.accept()\n"
        "    connection.close()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    process_lifetime.callback(terminate_process, process)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"测试 listener 提前退出: port={port}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return process
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"测试 listener 未能监听端口: {port}")


def _start_idle_process(process_lifetime: ExitStack) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    process_lifetime.callback(terminate_process, process)
    return process


def _handle(
    process: subprocess.Popen[str],
    port: int,
    owner: Literal["gateway", "backend"],
    workspace_root: Path,
) -> GatewayProcess | BackendProcess:
    if owner == "gateway":
        return GatewayProcess(
            process=process,
            stdout_file=StringIO(),
            stderr_file=StringIO(),
            port=port,
        )
    return BackendProcess(
        process=process,
        stdout_file=StringIO(),
        stderr_file=StringIO(),
        port=port,
        workspace_root=str(workspace_root),
        workspace_id="00000000-0000-4000-8000-000000000001",
    )


@pytest.mark.parametrize("owner", ["gateway", "backend"])
def test_process_scope_start_failure_preserves_replacement_port_owner(
    process_lifetime: ExitStack,
    owner: Literal["gateway", "backend"],
    tmp_path: Path,
) -> None:
    port = _free_port()
    original_process = _start_listener(port, process_lifetime=process_lifetime)
    handle = _handle(original_process, port, owner, tmp_path)
    replacement_process: subprocess.Popen[str] | None = None
    close = close_gateway_process if owner == "gateway" else close_backend_process

    with (
        pytest.raises(RuntimeError, match="模拟测试启动失败"),
        capture_test_processes(),
    ):
        register_test_process(handle, lambda: close(handle))
        original_process.terminate()
        original_process.wait(timeout=5)
        replacement_process = _start_listener(port, process_lifetime=process_lifetime)

        with pytest.raises(RuntimeError, match="测试端口已被占用"):
            if owner == "gateway":
                start_gateway_process(
                    workspace_root=tmp_path,
                    default_backend_url=None,
                    port=port,
                )
            else:
                start_backend_process(
                    workspace_root=str(tmp_path),
                    port=port,
                    log_name="occupied-backend",
                )

        raise RuntimeError("模拟测试启动失败")

    assert original_process.poll() is not None
    assert replacement_process is not None
    assert replacement_process.poll() is None
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        pass


@pytest.mark.parametrize("service", ["terminal", "navigation-scope"])
def test_backend_start_preserves_existing_port_owner(
    process_lifetime: ExitStack,
    service: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    listener = _start_listener(port, process_lifetime=process_lifetime)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("端口占用时不应创建进程"),
    )
    with pytest.raises(RuntimeError, match="测试端口已被占用"):
        if service == "terminal":
            terminal_backend.start_terminal_plugin_service(
                backend_port=port, workspace_root=str(tmp_path),
                artifacts_dir=tmp_path / "artifacts",
            )
        elif service == "navigation-scope":
            navigation_case._start_navigation_scope_backend(workspace_root=tmp_path, port=port)
    assert listener.poll() is None
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        pass
