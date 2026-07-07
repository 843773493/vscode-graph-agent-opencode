from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import time
from pathlib import Path
from typing import IO
from urllib.request import urlopen

E2E_READY_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class E2EBackendProcess:
    process: subprocess.Popen[str]
    stdout_file: IO[str]
    stderr_file: IO[str]
    port: int
    workspace_root: str


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    try:
        process.terminate()
        process.wait(timeout=10)
        return
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=10)
            return
        except ProcessLookupError:
            return
        except Exception as error:
            raise RuntimeError(
                f"无法强制终止进程: pid={process.pid}, returncode={process.poll()}"
            ) from error
    except Exception as error:
        raise RuntimeError(
            f"无法终止进程: pid={process.pid}, returncode={process.poll()}"
        ) from error


def kill_process_on_port(port: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"$connections = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue; "
                    "$connections | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique | "
                    "ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction Stop }",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                "清理监听端口失败: "
                f"port={port}, returncode={error.returncode}, stderr={error.stderr.decode('utf-8', errors='ignore')}"
            ) from error
        return

    try:
        subprocess.run(
            ["sh", "-c", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "清理监听端口失败: "
            f"port={port}, returncode={error.returncode}, stderr={error.stderr.decode('utf-8', errors='ignore')}"
        ) from error


def resolve_workspace_python_executable(project_root: Path) -> Path:
    windows_python = project_root / ".venv" / "Scripts" / "python.exe"
    if windows_python.exists():
        return windows_python

    posix_python = project_root / ".venv" / "bin" / "python"
    if posix_python.exists():
        return posix_python

    raise FileNotFoundError(
        f"未找到工作区虚拟环境 Python，可尝试路径: {windows_python} 或 {posix_python}"
    )


def wait_for_backend_ready(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + E2E_READY_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{port}/api/v1/health"

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"后端进程提前退出: pid={process.pid}, returncode={process.returncode}, port={port}"
            )
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)

    raise TimeoutError(f"后端在 {E2E_READY_TIMEOUT_SECONDS} 秒内未就绪，端口: {port}")


def start_backend_process(
    *,
    workspace_root: str,
    port: int,
    log_name: str,
    debugpy_port: int | None = None,
) -> E2EBackendProcess:
    kill_process_on_port(port)
    if debugpy_port is not None:
        kill_process_on_port(debugpy_port)

    project_root = Path.cwd().resolve()
    python_executable = resolve_workspace_python_executable(project_root)
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [str(python_executable)]
    if debugpy_port is not None:
        cmd.extend([
            "-m",
            "debugpy",
            "--listen",
            f"127.0.0.1:{debugpy_port}",
            "--wait-for-client",
        ])
    cmd.extend([
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ])

    log_dir = Path(workspace_root) / ".boxteam" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_file = open(log_dir / f"{log_name}.stdout.log", "a", encoding="utf-8")
    stderr_file = open(log_dir / f"{log_name}.stderr.log", "a", encoding="utf-8")

    process = subprocess.Popen(
        cmd,
        cwd=project_root,
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    handle = E2EBackendProcess(
        process=process,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        port=port,
        workspace_root=workspace_root,
    )
    try:
        wait_for_backend_ready(port, process)
    except Exception:
        close_backend_process(handle)
        raise

    return handle


def close_backend_process(handle: E2EBackendProcess) -> None:
    try:
        terminate_process(handle.process)
        kill_process_on_port(handle.port)
    finally:
        handle.stdout_file.close()
        handle.stderr_file.close()
