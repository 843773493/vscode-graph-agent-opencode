"""显式 Node runtime 下的原宿主协议与真实扩展原型。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def node_host_runtime() -> tuple[str, str]:
    executable = os.environ.get("BOXTEAM_TEST_NODE_HOST_EXECUTABLE")
    modules = os.environ.get("BOXTEAM_TEST_NODE_HOST_MODULES")
    if not executable or not modules:
        pytest.skip("UNMET_PREREQUISITE: 需显式提供匹配固定上游的 Node runtime 和已安装宿主依赖")
    assert Path(executable).is_file(), f"Node runtime 不存在：{executable}"
    assert Path(modules).is_dir(), f"Node 宿主依赖不存在：{modules}"
    return executable, modules


def test_original_node_host_protocol_and_dual_native_extensions(
    node_host_runtime: tuple[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Python 与 Node 共用运行身份，避免各自按进程 PID 建立不同证据目录。
    monkeypatch.setenv("BOXTEAM_TEST_RUN_ID", f"node-host-{uuid4()}")
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    executable, modules = node_host_runtime
    supervisor = context.project_root / "app/runtime" / (
        "windows_job.py" if sys.platform == "win32" else "posix_job.py"
    )
    command = [
        sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
        "--timeout-seconds", "45", "--", executable,
        "tests/integration/workspace_services/extension_host/node_host.mjs",
    ]
    result = subprocess.run(
        command, cwd=context.project_root,
        env={**os.environ, "BOXTEAM_TEST_NODE_HOST_EXECUTABLE": executable,
             "BOXTEAM_TEST_NODE_HOST_MODULES": modules},
        capture_output=True, timeout=55, check=False,
    )
    (context.artifacts_dir / "node-host.stdout.raw").write_bytes(result.stdout)
    (context.artifacts_dir / "node-host.stderr.raw").write_bytes(result.stderr)
    (context.artifacts_dir / "node-host-process.json").write_text(
        json.dumps({"command": command, "exit_code": result.returncode}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="replace")
    receipt = json.loads((context.artifacts_dir / "node-host-checks.json").read_text(encoding="utf-8"))
    hosts = {host["label"]: host for host in receipt["receipts"]}
    assert hosts["A"]["result"]["native"] == hosts["A"]["result"]["api"] == "A"
    assert hosts["B"]["result"]["native"] == hosts["B"]["result"]["api"] == "B"
    assert hosts["A"]["exit"] == hosts["B"]["exit"] == [0, None]
    assert hosts["A"]["errors"] == hosts["B"]["errors"] == []
