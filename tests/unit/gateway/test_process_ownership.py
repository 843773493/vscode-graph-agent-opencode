from __future__ import annotations

import socket
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path
from typing import Literal

import pytest

from tests.harness.python import browser_manager
from tests.harness.python.browser_manager import (
    BrowserFrontendProcess,
    BrowserManagerProcesses,
    close_browser_frontend_process,
    close_browser_manager_processes,
    start_browser_frontend_process,
    start_browser_manager_processes,
)
from tests.support.gateway_processes import (
    GatewayProcess,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.process_scope import capture_test_processes, register_test_process
from tests.support.processes import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_listener(port: int) -> subprocess.Popen[str]:
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
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"测试 listener 提前退出: port={port}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return process
        except OSError:
            time.sleep(0.02)
    process.terminate()
    process.wait(timeout=5)
    raise TimeoutError(f"测试 listener 未能监听端口: {port}")


def _start_idle_process() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


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
    owner: Literal["gateway", "backend"],
    tmp_path: Path,
) -> None:
    port = _free_port()
    original_process = _start_listener(port)
    handle = _handle(original_process, port, owner, tmp_path)
    replacement_process: subprocess.Popen[str] | None = None
    close = close_gateway_process if owner == "gateway" else close_backend_process

    try:
        with (
            pytest.raises(RuntimeError, match="模拟测试启动失败"),
            capture_test_processes(),
        ):
            register_test_process(handle, lambda: close(handle))
            original_process.terminate()
            original_process.wait(timeout=5)
            replacement_process = _start_listener(port)

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
    finally:
        if original_process.poll() is None:
            original_process.terminate()
            original_process.wait(timeout=5)
        if replacement_process is not None and replacement_process.poll() is None:
            replacement_process.terminate()
            replacement_process.wait(timeout=5)


@pytest.mark.parametrize("occupied_start", ["backend", "frontend", "frontend-only"])
def test_browser_manager_start_refuses_occupied_port_without_killing_owner(
    occupied_start: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_port = _free_port()
    frontend_port = _free_port()
    while frontend_port == backend_port:
        frontend_port = _free_port()
    occupied_port = backend_port if occupied_start == "backend" else frontend_port
    listener = _start_listener(occupied_port)
    monkeypatch.setattr(browser_manager.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(
        browser_manager.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("占用端口时不得启动子进程"),
    )

    try:
        with pytest.raises(RuntimeError, match="测试端口已被占用"):
            if occupied_start == "frontend-only":
                start_browser_frontend_process(
                    workspace_root=tmp_path,
                    frontend_port=frontend_port,
                )
            else:
                start_browser_manager_processes(
                    workspace_root=tmp_path,
                    backend_port=backend_port,
                    frontend_port=frontend_port,
                    backend_host="127.0.0.1",
                )
        assert listener.poll() is None
        with socket.create_connection(("127.0.0.1", occupied_port), timeout=1):
            pass
    finally:
        if listener.poll() is None:
            listener.terminate()
            listener.wait(timeout=5)


@pytest.mark.parametrize("close_frontend_only", [False, True])
def test_browser_manager_close_terminates_owned_processes_only(
    close_frontend_only: bool,
) -> None:
    backend_port = _free_port()
    frontend_port = _free_port()
    while frontend_port == backend_port:
        frontend_port = _free_port()
    backend_replacement = _start_listener(backend_port)
    frontend_replacement = _start_listener(frontend_port)
    owned_backend = _start_idle_process()
    owned_frontend = _start_idle_process()

    try:
        if close_frontend_only:
            close_browser_frontend_process(
                BrowserFrontendProcess(process=owned_frontend, port=frontend_port)
            )
            assert owned_frontend.poll() is not None
            assert owned_backend.poll() is None
        else:
            close_browser_manager_processes(
                BrowserManagerProcesses(
                    backend_process=owned_backend,
                    frontend_process=owned_frontend,
                    backend_port=backend_port,
                    frontend_port=frontend_port,
                )
            )
            assert owned_frontend.poll() is not None
            assert owned_backend.poll() is not None
        assert backend_replacement.poll() is None
        assert frontend_replacement.poll() is None
        for port in (backend_port, frontend_port):
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
    finally:
        for process in (
            owned_frontend,
            owned_backend,
            frontend_replacement,
            backend_replacement,
        ):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_browser_manager_frontend_start_failure_terminates_started_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_port = _free_port()
    frontend_port = _free_port()
    while frontend_port == backend_port:
        frontend_port = _free_port()
    real_popen = subprocess.Popen
    started: list[subprocess.Popen[str]] = []

    def fail_frontend_start(*_args: object, **_kwargs: object) -> subprocess.Popen[str]:
        if started:
            raise RuntimeError("模拟第二进程启动失败")
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        started.append(process)
        return process

    monkeypatch.setattr(browser_manager.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(
        browser_manager,
        "load_or_create_workspace_id",
        lambda _workspace_root: "00000000-0000-4000-8000-000000000001",
    )
    monkeypatch.setattr(browser_manager.subprocess, "Popen", fail_frontend_start)

    try:
        with pytest.raises(RuntimeError, match="模拟第二进程启动失败"):
            start_browser_manager_processes(
                workspace_root=tmp_path,
                backend_port=backend_port,
                frontend_port=frontend_port,
                backend_host="127.0.0.1",
            )

        assert len(started) == 1
        assert started[0].poll() is not None
    finally:
        for process in started:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_browser_manager_close_reports_all_owned_cleanup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_process = object()
    backend_process = object()
    attempted: list[object] = []

    def fail_cleanup(process: object) -> None:
        attempted.append(process)
        raise RuntimeError(f"模拟清理失败 {len(attempted)}")

    monkeypatch.setattr(browser_manager, "terminate_process", fail_cleanup)
    with pytest.raises(BaseExceptionGroup) as failure:
        close_browser_manager_processes(
            BrowserManagerProcesses(
                backend_process=backend_process,  # type: ignore[arg-type]
                frontend_process=frontend_process,  # type: ignore[arg-type]
                backend_port=18041,
                frontend_port=18042,
            )
        )

    assert attempted == [frontend_process, backend_process]
    assert [str(error) for error in failure.value.exceptions] == [
        "模拟清理失败 1",
        "模拟清理失败 2",
    ]
