"""首次默认 Quick Open 的文件分支经真实根元数据完成。"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import e2e_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest) -> str:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    return str(prepare_test_workspace(
        workspace_root=context.output_root / "workspace",
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",),
    ))


@pytest.fixture
def file_branch_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    block = e2e_port_block_for_file(Path(request.node.path))
    lease, probes = E2EProcessLease.acquire_available(
        context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json",
        range(block.port(20), block.end + 1),
    )
    (context.artifacts_dir / "port-allocation.json").write_text(json.dumps({
        "block": {"start": block.start, "end": block.end}, "port": lease.ports[0], "probes": probes,
    }, indent=2) + "\n", encoding="utf-8")
    try:
        yield lease.ports[0]
    finally:
        lease.release()


async def test_first_default_quick_open_completes_file_branch(
    request: pytest.FixtureRequest, e2e_workspace_root_path: str, e2e_workspace_config_path: str,
    file_branch_gateway_port: int, web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "首次文件快速访问验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    result_path = context.artifacts_dir / "file-branch.json"
    gateway = await asyncio.to_thread(
        start_gateway_process, workspace_root=Path(e2e_workspace_root_path), default_backend_url=None,
        port=file_branch_gateway_port, boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
    )
    command = ["bun", "tests/e2e/clients/web/quick_access_file_branch.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised_command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                          "--timeout-seconds", "90", "--", *command]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(
                subprocess.run, supervised_command, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=120, check=False,
            )
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
            "command": command, "supervised_command": supervised_command, "exit_code": completed.returncode,
        }, indent=2) + "\n", encoding="utf-8")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text(encoding="utf-8"))
        assert receipt["fileBranchCompleted"] is True
        assert receipt["pageErrors"] == []
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
