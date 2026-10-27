"""在 Windows Job Object 内运行 npm.cmd 安装执行链路测试。"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="需要原生 Windows .cmd 执行语义",
)


def test_windows_npm_cmd_execution_is_bounded() -> None:
    from app.runtime.windows_job import run_windows_job

    exit_code = run_windows_job(
        ["bun", "test", "scripts/install/windows-npm-execution.test.mjs"],
        timeout_seconds=60,
        memory_bytes=1024**3,
        timeout_message="Windows npm.cmd 执行验证超时",
        memory_limit_message="Windows npm.cmd 执行验证内存超限",
    )

    assert exit_code == 0
