from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.request import urlopen

from configs.installer import install_user_configuration

TEST_READY_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class BackendProcess:
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
        powershell_command = (
            "$connections = Get-NetTCPConnection "
            f"-LocalPort {port} -State Listen -ErrorAction SilentlyContinue; "
            "$connections | Select-Object -ExpandProperty OwningProcess | "
            "Sort-Object -Unique | ForEach-Object { "
            "Stop-Process -Id $_ -Force -ErrorAction Stop }"
        )
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    powershell_command,
                ],
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            stderr = error.stderr.decode("utf-8", errors="ignore")
            raise RuntimeError(
                "清理监听端口失败: "
                f"port={port}, returncode={error.returncode}, stderr={stderr}"
            ) from error
        return

    try:
        subprocess.run(
            ["sh", "-c", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(
            "清理监听端口失败: "
            f"port={port}, returncode={error.returncode}, stderr={stderr}"
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
    deadline = time.monotonic() + TEST_READY_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{port}/api/v1/health"

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "后端进程提前退出: "
                f"pid={process.pid}, returncode={process.returncode}, port={port}"
            )
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)

    raise TimeoutError(f"后端在 {TEST_READY_TIMEOUT_SECONDS} 秒内未就绪，端口: {port}")


def wait_for_http_ok(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + TEST_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "测试进程提前退出: "
                f"pid={process.pid}, returncode={process.returncode}, url={url}"
            )
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)

    raise TimeoutError(f"测试服务在 {TEST_READY_TIMEOUT_SECONDS} 秒内未就绪: {url}")


def start_backend_process(
    *,
    workspace_root: str,
    port: int,
    log_name: str,
    debugpy_port: int | None = None,
    env_overrides: dict[str, str] | None = None,
) -> BackendProcess:
    kill_process_on_port(port)
    if debugpy_port is not None:
        kill_process_on_port(debugpy_port)

    project_root = Path.cwd().resolve()
    python_executable = resolve_workspace_python_executable(project_root)
    boxteam_home = Path(workspace_root).resolve().parent / "boxteam-home"
    install_user_configuration(
        config_root=boxteam_home / "config",
        profile="default",
        project_root=project_root,
    )
    workspace_config_path = Path(workspace_root) / ".boxteam" / "workspace.jsonc"
    if not workspace_config_path.is_file():
        raise FileNotFoundError(
            f"启动测试后端前必须先复制工作区配置: {workspace_config_path}"
        )
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root
    env["BOXTEAM_HOME"] = str(boxteam_home)
    env["PYTHONUNBUFFERED"] = "1"
    if env_overrides:
        env.update(env_overrides)

    cmd = [str(python_executable)]
    if debugpy_port is not None:
        cmd.extend(
            [
                "-m",
                "debugpy",
                "--listen",
                f"127.0.0.1:{debugpy_port}",
                "--wait-for-client",
            ]
        )
    cmd.extend(
        [
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
    )

    log_dir = Path(workspace_root) / ".boxteam" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # 文件句柄由 BackendProcess 持有，并在 close_backend_process 中统一关闭。
    stdout_file = open(  # noqa: SIM115
        log_dir / f"{log_name}.stdout.log", "a", encoding="utf-8"
    )
    stderr_file = open(  # noqa: SIM115
        log_dir / f"{log_name}.stderr.log", "a", encoding="utf-8"
    )

    process = subprocess.Popen(
        cmd,
        cwd=project_root,
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    handle = BackendProcess(
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


def close_backend_process(handle: BackendProcess) -> None:
    try:
        terminate_process(handle.process)
        kill_process_on_port(handle.port)
    finally:
        handle.stdout_file.close()
        handle.stderr_file.close()
