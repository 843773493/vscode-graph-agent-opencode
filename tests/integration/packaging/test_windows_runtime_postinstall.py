"""在原生 Windows Job Object 中验证 runtime postinstall 文件生命周期。"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="需要原生 Windows 文件路径和进程语义",
)


def test_windows_runtime_postinstall_is_bounded() -> None:
    from app.runtime.windows_job import run_windows_job

    exit_code = run_windows_job(
        ["bun", "test", "scripts/install/runtime-postinstall.test.mjs"],
        timeout_seconds=120,
        memory_bytes=1024**3,
        timeout_message="Windows runtime postinstall 验收超时",
        memory_limit_message="Windows runtime postinstall 验收内存超限",
    )

    assert exit_code == 0
