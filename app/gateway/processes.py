from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx


GATEWAY_PROCESS_READY_TIMEOUT_SECONDS = 45


@dataclass(slots=True)
class ManagedProcess:
    process: subprocess.Popen[str]
    log_file: object

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        close = getattr(self.log_file, "close", None)
        if callable(close):
            close()


def allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def resolve_python_executable(project_root: Path) -> Path:
    configured = os.environ.get("PYTHON_BIN")
    if configured:
        return Path(configured).expanduser().resolve()

    windows_python = project_root / ".venv" / "Scripts" / "python.exe"
    if windows_python.exists():
        return windows_python

    posix_python = project_root / ".venv" / "bin" / "python"
    if posix_python.exists():
        return posix_python

    raise FileNotFoundError(
        f"未找到项目 Python 解释器，已检查: {windows_python} 和 {posix_python}"
    )


async def wait_for_http_ok(url: str, process: subprocess.Popen[str] | None = None) -> None:
    deadline = asyncio.get_running_loop().time() + GATEWAY_PROCESS_READY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=2) as client:
        while asyncio.get_running_loop().time() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError(
                    f"进程提前退出: pid={process.pid}, returncode={process.returncode}, url={url}"
                )
            try:
                response = await client.get(url, headers={"X-Local-Token": "local-dev-token"})
                if response.status_code == 200:
                    return
                last_error = RuntimeError(
                    f"健康检查返回 {response.status_code}: {response.text[:300]}"
                )
            except Exception as error:
                last_error = error
            await asyncio.sleep(0.5)

    detail = f"，最后错误: {last_error}" if last_error else ""
    raise TimeoutError(f"目标服务在 {GATEWAY_PROCESS_READY_TIMEOUT_SECONDS} 秒内未就绪: {url}{detail}")


def start_local_backend_process(
    *,
    project_root: Path,
    workspace_root: Path,
    port: int,
    log_dir: Path,
) -> ManagedProcess:
    python_executable = resolve_python_executable(project_root)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / f"local-backend-{port}.log", "a", encoding="utf-8")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            str(python_executable),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=project_root,
        env=env,
        stdout=log_file,
        stderr=log_file,
        text=True,
    )
    return ManagedProcess(process=process, log_file=log_file)


def start_ssh_tunnel_process(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: Path,
    local_port: int,
    remote_backend_host: str,
    remote_backend_port: int,
    log_dir: Path,
) -> ManagedProcess:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / f"ssh-tunnel-{local_port}.log", "a", encoding="utf-8")
    process = subprocess.Popen(
        [
            "ssh",
            "-N",
            "-L",
            f"127.0.0.1:{local_port}:{remote_backend_host}:{remote_backend_port}",
            "-i",
            str(private_key_path),
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"{username}@{host}",
        ],
        stdout=log_file,
        stderr=log_file,
        text=True,
    )
    return ManagedProcess(process=process, log_file=log_file)
