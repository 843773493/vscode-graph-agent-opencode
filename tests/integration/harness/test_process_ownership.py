from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack
from io import StringIO
from pathlib import Path
from typing import Literal, TextIO

import pytest

from tests.harness.python import browser_manager, terminal_backend, terminal_frontend
from tests.harness.python.browser_manager import (
    BrowserFrontendProcess,
    BrowserManagerProcesses,
    close_browser_frontend_process,
    close_browser_manager_processes,
    start_browser_frontend_process,
    start_browser_manager_processes,
)
from tests.harness.python.terminal_frontend import (
    TerminalFrontendProcess,
    close_terminal_frontend_process,
    start_terminal_frontend_process,
)
from tests.integration.gateway import (
    test_navigation_actor_scope_proxy as navigation_case,
)
from tests.integration.workspace_services.browser import (
    test_browser_resource_performance as performance_case,
)
from tests.support.processes.gateway import (
    GatewayProcess,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.scope import capture_test_processes, register_test_process
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
    terminate_process,
)


@pytest.fixture
def loud_auxiliary_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[subprocess.Popen[str]]]:
    """只替换子进程程序，保留装具实际配置的输出载体与就绪路径。"""
    created: list[subprocess.Popen[str]] = []
    script = """
import os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
os.write(1, b'stdout-payload\\n' * 32768)
os.write(2, b'stderr-payload\\n' * 32768)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args):
        pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""

    class LoudAuxiliaryProcess(subprocess.Popen[str]):
        def __init__(
            self,
            args: list[str],
            *,
            cwd: Path,
            env: dict[str, str],
            stdout: int | TextIO,
            stderr: int | TextIO,
            text: bool,
        ) -> None:
            port = args[args.index("--port") + 1]
            super().__init__(
                [sys.executable, "-u", "-c", script, port],
                cwd=cwd, env=env, stdout=stdout, stderr=stderr, text=text,
            )
            created.append(self)

    wait_for_http_ok = browser_manager.wait_for_http_ok

    def short_readiness(url: str, process: subprocess.Popen[str], **_kwargs: object) -> None:
        wait_for_http_ok(url, process, timeout_seconds=0.5)

    monkeypatch.setattr(browser_manager.subprocess, "Popen", LoudAuxiliaryProcess)
    monkeypatch.setattr(browser_manager, "wait_for_http_ok", short_readiness)
    monkeypatch.setattr(terminal_frontend, "wait_for_http_ok", short_readiness)
    monkeypatch.setattr(terminal_backend, "wait_for_http_ok", short_readiness)
    yield created
    for process in created:
        browser_manager.terminate_process(process)


@pytest.mark.parametrize("process_kind", ["manager", "frontend", "terminal-frontend", "terminal-backend"])
def test_large_startup_output_does_not_block_auxiliary_fixture(
    process_kind: str,
    loud_auxiliary_processes: list[subprocess.Popen[str]],
    integration_workspace_root_path: str,
) -> None:
    workspace_root = Path(integration_workspace_root_path)
    workspace = workspace_root / "nested" / process_kind
    artifacts = workspace_root.parent / "artifacts"
    previous_logs = set(artifacts.glob("*.log"))
    ports = integration_port_block_for_file(Path(__file__))
    with ExitStack() as lifetime:
        if process_kind == "manager":
            handle = start_browser_manager_processes(
                workspace_root=workspace, backend_port=ports.port(64),
                frontend_port=ports.port(65), backend_host="127.0.0.1",
                artifacts_dir=artifacts,
            )
            lifetime.callback(close_browser_manager_processes, handle)
        elif process_kind == "frontend":
            frontend = start_browser_frontend_process(
                workspace_root=workspace, frontend_port=ports.port(65),
                artifacts_dir=artifacts,
            )
            lifetime.callback(close_browser_frontend_process, frontend)
        elif process_kind == "terminal-frontend":
            terminal = start_terminal_frontend_process(
                workspace_root=workspace, frontend_port=ports.port(65),
                artifacts_dir=artifacts,
            )
            lifetime.callback(close_terminal_frontend_process, terminal)
        else:
            backend = terminal_backend.start_terminal_backend(
                workspace_root=str(workspace), backend_port=ports.port(65),
                artifacts_dir=artifacts,
            )
            lifetime.callback(terminal_backend.terminate_process, backend)
        expected_process_count = 2 if process_kind == "manager" else 1
        assert len(loud_auxiliary_processes) == expected_process_count
        assert all(process.poll() is None for process in loud_auxiliary_processes)
        assert all(process.stdout is None and process.stderr is None for process in loud_auxiliary_processes)

    logs = set(artifacts.glob("*.log")) - previous_logs
    assert not (workspace.parent / "artifacts").exists()
    assert len(logs) == expected_process_count * 2
    for channel in ("stdout", "stderr"):
        channel_logs = [path for path in logs if path.name.endswith(f".{channel}.log")]
        assert len(channel_logs) == expected_process_count
        for path in channel_logs:
            assert path.read_bytes() == f"{channel}-payload\n".encode() * 32768
    assert all(process.poll() is not None for process in loud_auxiliary_processes)


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


@pytest.mark.parametrize(
    "occupied_start", ["backend", "frontend", "frontend-only", "terminal-frontend"]
)
def test_auxiliary_start_refuses_occupied_port_without_killing_owner(
    process_lifetime: ExitStack,
    occupied_start: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_port = _free_port()
    frontend_port = _free_port()
    while frontend_port == backend_port:
        frontend_port = _free_port()
    occupied_port = backend_port if occupied_start == "backend" else frontend_port
    listener = _start_listener(occupied_port, process_lifetime=process_lifetime)
    monkeypatch.setattr(browser_manager.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(
        browser_manager.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("占用端口时不得启动子进程"),
    )

    with pytest.raises(RuntimeError, match="测试端口已被占用"):
        if occupied_start == "terminal-frontend":
            start_terminal_frontend_process(
                workspace_root=tmp_path,
                artifacts_dir=tmp_path / "artifacts",
                frontend_port=frontend_port,
            )
        elif occupied_start == "frontend-only":
            start_browser_frontend_process(
                workspace_root=tmp_path,
                artifacts_dir=tmp_path / "artifacts",
                frontend_port=frontend_port,
            )
        else:
            start_browser_manager_processes(
                workspace_root=tmp_path,
                artifacts_dir=tmp_path / "artifacts",
                backend_port=backend_port,
                frontend_port=frontend_port,
                backend_host="127.0.0.1",
            )
    assert listener.poll() is None
    with socket.create_connection(("127.0.0.1", occupied_port), timeout=1):
        pass


@pytest.mark.parametrize("service", ["browser-manager", "browser-frontend", "terminal-frontend"])
def test_auxiliary_close_terminates_owned_processes_only(
    process_lifetime: ExitStack,
    service: str,
) -> None:
    backend_port = _free_port()
    frontend_port = _free_port()
    while frontend_port == backend_port:
        frontend_port = _free_port()
    backend_replacement = _start_listener(backend_port, process_lifetime=process_lifetime)
    frontend_replacement = _start_listener(frontend_port, process_lifetime=process_lifetime)
    owned_backend = _start_idle_process(process_lifetime)
    owned_frontend = _start_idle_process(process_lifetime)

    if service == "terminal-frontend":
        close_terminal_frontend_process(
            TerminalFrontendProcess(process=owned_frontend, port=frontend_port)
        )
        assert owned_frontend.poll() is not None
        assert owned_backend.poll() is None
    elif service == "browser-frontend":
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


def test_browser_manager_frontend_start_failure_terminates_started_backend(
    process_lifetime: ExitStack,
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
        process_lifetime.callback(terminate_process, process)
        started.append(process)
        return process

    monkeypatch.setattr(browser_manager.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(
        browser_manager,
        "load_or_create_workspace_id",
        lambda _workspace_root: "00000000-0000-4000-8000-000000000001",
    )
    monkeypatch.setattr(browser_manager.subprocess, "Popen", fail_frontend_start)

    with pytest.raises(RuntimeError, match="模拟第二进程启动失败"):
        start_browser_manager_processes(
            workspace_root=tmp_path,
            artifacts_dir=tmp_path / "artifacts",
            backend_port=backend_port,
            frontend_port=frontend_port,
            backend_host="127.0.0.1",
        )

    assert len(started) == 1
    assert started[0].poll() is not None


@pytest.mark.parametrize("service", ["terminal", "navigation-scope", "browser-performance"])
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
            terminal_backend.start_terminal_backend(backend_port=port, workspace_root=str(tmp_path), artifacts_dir=tmp_path / "artifacts")
        elif service == "navigation-scope":
            navigation_case._start_navigation_scope_backend(workspace_root=tmp_path, port=port)
        else:
            performance_case._browser_backend_process(
                workspace_root=tmp_path, backend_port=port, log_path=tmp_path / "browser.log",
            )
    assert listener.poll() is None
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        pass
