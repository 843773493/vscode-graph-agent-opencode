"""用共享 Windows Job Object helper 限制测试并回收全部后代。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
windows_job = importlib.import_module("app.runtime.windows_job")

TIMEOUT_SECONDS = 600
MEMORY_BYTES = 4 * 1024**3


def run_command(
    command: list[str],
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
    memory_bytes: int = MEMORY_BYTES,
) -> int:
    return windows_job.run_windows_job(
        command,
        timeout_seconds=timeout_seconds,
        memory_bytes=memory_bytes,
        timeout_message=f"Windows 测试超时: {timeout_seconds}s",
        memory_limit_message=f"Windows 测试进程树内存超限: limit={memory_bytes}",
    )


if __name__ == "__main__":
    raise SystemExit(run_command([sys.executable, "-m", "pytest", *sys.argv[1:]]))
