from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.request import Request, urlopen

import httpx

from tests.e2e.processes import (
    kill_process_on_port,
    resolve_workspace_python_executable,
    terminate_process,
)


LOCAL_TOKEN_HEADERS = {"X-Local-Token": "local-dev-token"}
READY_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class GatewayProcess:
    process: subprocess.Popen[str]
    stdout_file: IO[str]
    stderr_file: IO[str]
    port: int


def wait_for_gateway_ready(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    request = Request(
        f"http://127.0.0.1:{port}/api/gateway/health",
        headers=LOCAL_TOKEN_HEADERS,
    )
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Gateway 提前退出: pid={process.pid}, returncode={process.returncode}"
            )
        try:
            with urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise TimeoutError(f"Gateway 在 {READY_TIMEOUT_SECONDS} 秒内未就绪: {port}")


def start_gateway_process(
    *,
    workspace_root: Path,
    default_backend_url: str,
    port: int,
    ssh_tunnel_port_range: tuple[int, int] | None = None,
) -> GatewayProcess:
    kill_process_on_port(port)
    project_root = Path.cwd().resolve()
    python_executable = resolve_workspace_python_executable(project_root)
    log_dir = workspace_root / ".boxteam" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_file = open(log_dir / "gateway.stdout.log", "a", encoding="utf-8")
    stderr_file = open(log_dir / "gateway.stderr.log", "a", encoding="utf-8")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    env["BOXTEAM_GATEWAY_ROOT"] = str(workspace_root / ".boxteam" / "gateway")
    env["BOXTEAM_DEFAULT_BACKEND_URL"] = default_backend_url
    env["BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT"] = str(workspace_root)
    env["PYTHONUNBUFFERED"] = "1"
    if ssh_tunnel_port_range is not None:
        env["BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN"] = str(ssh_tunnel_port_range[0])
        env["BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX"] = str(ssh_tunnel_port_range[1])
    process = subprocess.Popen(
        [
            str(python_executable),
            "-m",
            "uvicorn",
            "app.gateway.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=project_root,
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    handle = GatewayProcess(
        process=process,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        port=port,
    )
    try:
        wait_for_gateway_ready(port, process)
    except Exception:
        close_gateway_process(handle)
        raise
    return handle


def close_gateway_process(handle: GatewayProcess) -> None:
    try:
        terminate_process(handle.process)
        kill_process_on_port(handle.port)
    finally:
        handle.stdout_file.close()
        handle.stderr_file.close()


def workspace_root_from_response(response: httpx.Response) -> str:
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["root_path"])


def write_gateway_ssh_workspace_config(
    *,
    workspace_root: Path,
    ssh_port: int,
    username: str,
    remote_backend_port: int,
    remote_workspace_path: str,
) -> None:
    boxteam_dir = workspace_root / ".boxteam"
    boxteam_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "gateway": {
            "workspaces": [
                {
                    "kind": "ssh",
                    "name": "remote docker",
                    "host": "127.0.0.1",
                    "port": ssh_port,
                    "username": username,
                    "private_key_path": "asset/gateway_ssh/id_ed25519",
                    "remote_backend_host": "127.0.0.1",
                    "remote_backend_port": remote_backend_port,
                    "remote_workspace_path": remote_workspace_path,
                    "activate": False,
                }
            ]
        }
    }
    (boxteam_dir / "boxteam.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
