from __future__ import annotations

import socket
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path
from typing import Literal

import pytest

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
