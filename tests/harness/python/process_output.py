"""Browser、Terminal 辅助服务测试进程的日志输出载体。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4


def spawn_logged_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    artifacts_dir: Path,
    log_name: str,
) -> tuple[subprocess.Popen[str], tuple[Path, Path]]:
    """子进程直接持有日志描述符；父进程启动后即关闭自己的副本，不积压管道。"""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{log_name}-{uuid4().hex}"
    logs = (artifacts_dir / f"{prefix}.stdout.log", artifacts_dir / f"{prefix}.stderr.log")
    try:
        with open(logs[0], "x", encoding="utf-8") as stdout, open(logs[1], "x", encoding="utf-8") as stderr:
            process = subprocess.Popen(
                command, cwd=cwd, env=env, stdout=stdout, stderr=stderr, text=True,
            )
    except BaseException as error:
        for path in logs:
            error.add_note(f"辅助服务测试进程日志：{path}")
        raise
    return process, logs
