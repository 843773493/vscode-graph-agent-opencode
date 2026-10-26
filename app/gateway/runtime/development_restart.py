from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.gateway.runtime.process_lifecycle import _spawn_logged_process
from app.gateway.runtime.process_logs import ProcessLogStore

RESTART_DELAY_MS = 1_000


@dataclass(frozen=True, slots=True)
class DevelopmentRestartCommand:
    runner_path: Path
    script_path: Path
    working_directory: Path

    @property
    def argv(self) -> list[str]:
        return [
            str(self.runner_path),
            str(self.script_path),
            "--only-launch",
            f"--restart-delay-ms={RESTART_DELAY_MS}",
        ]


def resolve_development_restart_command() -> DevelopmentRestartCommand | None:
    runner_path_value = os.environ.get("BOXTEAM_DEVELOPMENT_RESTART_RUNNER")
    script_path_value = os.environ.get("BOXTEAM_DEVELOPMENT_RESTART_SCRIPT")
    working_directory_value = os.environ.get("BOXTEAM_DEVELOPMENT_RESTART_CWD")
    configured_values = (
        runner_path_value,
        script_path_value,
        working_directory_value,
    )
    if all(value is None for value in configured_values):
        return None
    if any(not value or not value.strip() for value in configured_values):
        raise RuntimeError("源码开发服务重启配置不完整")

    runner_path = Path(runner_path_value).resolve()
    script_path = Path(script_path_value).resolve()
    working_directory = Path(working_directory_value).resolve()
    if not runner_path.is_file():
        raise FileNotFoundError(f"源码开发服务重启运行器不存在: {runner_path}")
    if not script_path.is_file():
        raise FileNotFoundError(f"源码开发服务重启脚本不存在: {script_path}")
    if not working_directory.is_dir():
        raise NotADirectoryError(
            f"源码开发服务重启工作目录不存在: {working_directory}"
        )
    return DevelopmentRestartCommand(
        runner_path=runner_path,
        script_path=script_path,
        working_directory=working_directory,
    )


def start_development_restart(
    command: DevelopmentRestartCommand,
    *,
    log_dir: Path,
    environment: dict[str, str] | None = None,
) -> int:
    with ProcessLogStore(log_dir).open("development-restart") as log_file:
        process = _spawn_logged_process(
            command.argv,
            cwd=command.working_directory,
            env={**os.environ, **(environment or {})},
            log_file=log_file,
            detached=True,
        )
    if process.pid <= 0:
        raise RuntimeError("源码开发服务重启进程未返回有效 PID")
    return process.pid
