from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

RESTART_DELAY_MS = 1_000


@dataclass(frozen=True, slots=True)
class DevelopmentRestartCommand:
    runner: Path
    script: Path
    cwd: Path

    @property
    def argv(self) -> list[str]:
        return [
            str(self.runner),
            str(self.script),
            "--only-launch",
            f"--restart-delay-ms={RESTART_DELAY_MS}",
        ]


def resolve_development_restart_command() -> DevelopmentRestartCommand | None:
    runner_value = os.environ.get("BOXTEAM_DEVELOPMENT_RESTART_RUNNER")
    script_value = os.environ.get("BOXTEAM_DEVELOPMENT_RESTART_SCRIPT")
    cwd_value = os.environ.get("BOXTEAM_DEVELOPMENT_RESTART_CWD")
    configured_values = (runner_value, script_value, cwd_value)
    if all(value is None for value in configured_values):
        return None
    if any(not value or not value.strip() for value in configured_values):
        raise RuntimeError("源码开发服务重启配置不完整")

    runner = Path(runner_value).resolve()
    script = Path(script_value).resolve()
    cwd = Path(cwd_value).resolve()
    if not runner.is_file():
        raise FileNotFoundError(f"源码开发服务重启运行器不存在: {runner}")
    if not script.is_file():
        raise FileNotFoundError(f"源码开发服务重启脚本不存在: {script}")
    if not cwd.is_dir():
        raise NotADirectoryError(f"源码开发服务重启工作目录不存在: {cwd}")
    return DevelopmentRestartCommand(runner=runner, script=script, cwd=cwd)


def start_development_restart(
    command: DevelopmentRestartCommand,
    *,
    log_path: Path,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        platform_options: dict[str, object]
        if os.name == "nt":
            platform_options = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS,
            }
        else:
            platform_options = {"start_new_session": True}
        process = subprocess.Popen(
            command.argv,
            cwd=command.cwd,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            **platform_options,
        )
    if process.pid <= 0:
        raise RuntimeError("源码开发服务重启进程未返回有效 PID")
    return process.pid
