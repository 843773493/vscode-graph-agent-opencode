from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from app.core.mounted_workspaces import MountedWorkspace
from app.gateway.runtime.workspace_process import resolve_backend_workspace_id
from configs.installer import install_user_configuration
from tests.support.processes.scope import (
    register_test_process,
    unregister_test_process,
)

TEST_READY_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class BackendProcess:
    process: subprocess.Popen[str]
    stdout_file: IO[str]
    stderr_file: IO[str]
    port: int
    workspace_root: str
    workspace_id: str


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


def ensure_local_port_available(port: int, *, service_name: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(
                f"{service_name} 测试端口已被占用，拒绝终止未知进程: port={port}"
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


def workspace_backend_id_for_root(workspace_root: str | Path) -> str:
    project_root = Path.cwd().resolve()
    return resolve_backend_workspace_id(project_root, Path(workspace_root))


def wait_for_backend_ready(
    port: int,
    process: subprocess.Popen[str],
    workspace_id: str,
) -> None:
    """在总等待预算内取得 200 健康响应，并核对实际后端的工作区身份。"""
    started = time.monotonic()
    deadline = started + TEST_READY_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{port}/api/v1/health"
    request = Request(url, headers={"X-BoxTeam-Workspace-Id": workspace_id})
    last_error: OSError | None = None

    while (remaining := deadline - time.monotonic()) > 0:
        if process.poll() is not None:
            raise RuntimeError(
                "后端进程提前退出: "
                f"pid={process.pid}, returncode={process.returncode}, port={port}"
            )
        try:
            with urlopen(request, timeout=min(1, remaining)) as response:
                if response.status != 200:
                    raise RuntimeError(
                        "Workspace 健康响应不是 200: "
                        f"status={response.status}, workspace_id={workspace_id}, url={url}"
                    )
                payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise TypeError("Workspace 健康响应必须是 JSON 对象")
                if payload.get("workspace_id") != workspace_id:
                    raise RuntimeError(
                        "Workspace 测试后端 UUID 不匹配: "
                        f"expected={workspace_id}, "
                        f"actual={payload.get('workspace_id')!r}"
                    )
                return
        except HTTPError as error:
            with error:
                detail = error.read(1200).decode("utf-8", errors="replace")[:300]
            raise RuntimeError(
                "Workspace 健康探针被拒绝: "
                f"status={error.code}, workspace_id={workspace_id}, body={detail}"
            ) from error
        except OSError as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))

    raise TimeoutError(
        f"后端在 {TEST_READY_TIMEOUT_SECONDS} 秒内未就绪，端口: {port}; "
        f"pid={process.pid}, returncode={process.poll()}, elapsed={time.monotonic() - started:.3f}s, "
        f"health={url}, last_error={last_error!r}"
    ) from last_error


def wait_for_http_ok(
    url: str,
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float = TEST_READY_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
) -> None:
    """等待本地进程开始提供 200 健康响应；HTTP 拒绝直接暴露，不当作尚未监听。"""
    if timeout_seconds <= 0:
        raise ValueError("HTTP 就绪期限必须大于零")
    started = time.monotonic()
    deadline = started + timeout_seconds
    request = Request(url, headers=headers) if headers is not None else url
    last_error: OSError | None = None
    while (remaining := deadline - time.monotonic()) > 0:
        returncode = process.poll()
        if returncode is not None:
            capture_error: subprocess.TimeoutExpired | None = None
            try:
                stdout, stderr = process.communicate(timeout=min(1, remaining))
            except subprocess.TimeoutExpired as error:
                # 进程已退出不代表派生进程也关闭了管道；诊断超时不能覆盖退出事实。
                capture_error = error
                stdout, stderr = error.output, error.stderr
            stdout, stderr = (
                value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
                for value in (stdout, stderr)
            )
            failure = RuntimeError(
                "测试进程提前退出: "
                f"pid={process.pid}, returncode={returncode}, url={url}\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )
            if capture_error is not None:
                failure.add_note("输出采集超时；以上仅包含当前已捕获的输出，进程退出码来自 poll()。")
            raise failure from capture_error
        try:
            with urlopen(request, timeout=min(1, remaining)) as response:
                if response.status == 200:
                    return
                raise RuntimeError(
                    f"测试健康响应不是 200: status={response.status}, url={url}"
                )
        except HTTPError as error:
            error.close()
            raise RuntimeError(
                f"测试健康探针被拒绝: status={error.code}, url={url}"
            ) from error
        except OSError as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))

    raise TimeoutError(
        f"测试服务在 {timeout_seconds} 秒内未就绪: {url}; "
        f"pid={process.pid}, elapsed={time.monotonic() - started:.3f}s, "
        f"last_error={last_error}"
    ) from last_error


def start_backend_process(
    *,
    workspace_root: str,
    port: int,
    log_name: str,
    debugpy_port: int | None = None,
    asgi_app: str = "app.main:app",
    env_overrides: dict[str, str] | None = None,
    startup_mount: MountedWorkspace | None = None,
    backend_generation: str | None = None,
    env_unset: Sequence[str] = (),
    refresh_user_configuration: bool = True,
) -> BackendProcess:
    ensure_local_port_available(port, service_name="Workspace 后端")
    if debugpy_port is not None:
        ensure_local_port_available(debugpy_port, service_name="Workspace debugpy")

    project_root = Path.cwd().resolve()
    python_executable = resolve_workspace_python_executable(project_root)
    boxteam_home = Path(workspace_root).resolve().parent / "boxteam-home"
    install_user_configuration(
        config_root=boxteam_home / "config",
        profile="default",
        project_root=project_root,
        force=refresh_user_configuration,
    )
    workspace_config_path = Path(workspace_root) / ".boxteam" / "workspace.jsonc"
    if not workspace_config_path.is_file():
        raise FileNotFoundError(
            f"启动测试后端前必须先复制工作区配置: {workspace_config_path}"
        )
    env = os.environ.copy()
    env.pop("WORKSPACE_ROOT", None)
    env["BOXTEAM_HOME"] = str(boxteam_home)
    env["PYTHONUNBUFFERED"] = "1"
    for name in env_unset:
        env.pop(name, None)
    if env_overrides:
        env.update(env_overrides)
    backend_workspace_id = resolve_backend_workspace_id(
        project_root,
        Path(workspace_root),
    )
    mount = startup_mount or MountedWorkspace(
        backend_workspace_id, Path(workspace_root).resolve(),
    )
    if mount.workspace_id != backend_workspace_id or mount.workspace_root != Path(workspace_root).resolve():
        raise ValueError("测试 Backend 的 startup mount 与指定工作区身份不一致")
    env["BOXTEAM_MOUNTED_WORKSPACES"] = json.dumps(
        [mount.to_payload()], separators=(",", ":"),
    )
    env["BOXTEAM_BACKEND_GENERATION"] = backend_generation or f"test_backend_{uuid4().hex}"


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
            asgi_app,
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
    # 创建失败时回收已打开的日志；成功后交给 BackendProcess 持有。
    with ExitStack() as log_files:
        stdout_file = log_files.enter_context(open(
            log_dir / f"{log_name}.stdout.log", "a", encoding="utf-8",
        ))
        stderr_file = log_files.enter_context(open(
            log_dir / f"{log_name}.stderr.log", "a", encoding="utf-8",
        ))
        popen_at = datetime.now(UTC).isoformat()
        popen_started = time.monotonic()
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
            workspace_id=backend_workspace_id,
        )
        log_files.pop_all()
    try:
        wait_for_backend_ready(port, process, backend_workspace_id)
    except BaseException as startup_error:
        startup_error.add_note(
            f"Workspace 测试后端启动诊断: pid={process.pid}, Popen_utc={popen_at}, "
            f"elapsed={time.monotonic() - popen_started:.3f}s, workspace={workspace_root}, "
            f"stdout={log_dir / f'{log_name}.stdout.log'}, stderr={log_dir / f'{log_name}.stderr.log'}"
        )
        try:
            close_backend_process(handle)
        except BaseException as cleanup_error:  # noqa: BLE001 - 启动和清理错误一并保留
            raise BaseExceptionGroup(
                "Workspace 测试后端启动失败且清理已启动进程失败",
                [startup_error, cleanup_error],
            ) from None
        raise

    register_test_process(
        handle,
        lambda: close_backend_process(handle),
    )
    return handle


def close_backend_process(handle: BackendProcess) -> None:
    errors: list[BaseException] = []
    for cleanup in (
        lambda: terminate_process(handle.process),
        handle.stdout_file.close,
        handle.stderr_file.close,
    ):
        try:
            cleanup()
        except BaseException as error:  # noqa: BLE001 - 中断也须释放其余资源，再报告全部错误。
            errors.append(error)
    if errors:
        raise BaseExceptionGroup("Workspace 测试进程及日志清理失败", errors)
    unregister_test_process(handle)
