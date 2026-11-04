"""真实未保存文件覆盖、双工作区小配额与原 cursor 绑定。"""

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
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest) -> str:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    return str(prepare_test_workspace(
        workspace_root=context.output_root / "workspace",
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",),
    ))


@pytest.fixture
def coverage_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = e2e_port_block_for_file(Path(request.node.path)).port(20)
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", (port,))
    lease.acquire()
    try:
        yield port
    finally:
        lease.release()


async def test_dirty_file_coverage_keeps_other_owner_and_paginated_matches(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    coverage_gateway_port: int,
    web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("文件覆盖验收需要 Chromium")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = Path(e2e_workspace_root_path)
    template = context.project_root / "tests/fixtures/workspaces/default_test_workspace"
    workspaces = [prepare_test_workspace(workspace_root=root / name, template_root=template, template_items=())
                  for name in ("A", "B")]
    for target in workspaces:
        install_test_workspace_config(workspace_root=target, config_path=Path(e2e_workspace_config_path),
                                      schema_path=root / ".boxteam/workspace_schema.jsonc")
        (target / "00-dirty.txt").write_text(f"{target.name} disk coverageword\n", encoding="utf-8")
    (workspaces[0] / "10-first.txt").write_text("A first coverageword\n", encoding="utf-8")
    (workspaces[0] / "20-tail.txt").write_text("A tail coverageword\n", encoding="utf-8")
    (workspaces[0] / "nested").mkdir()
    nested_files = {"parent.txt": "partitionword parent disk", "nested/keep.md": "partitionword child disk",
                    "nested/drop.txt": "partitionword excluded child disk"}
    for name, body in nested_files.items():
        (workspaces[0] / name).write_text(body, encoding="utf-8")
    gateway = start_gateway_process(
        workspace_root=workspaces[0], default_backend_url=None, port=coverage_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
    )
    result_path = context.artifacts_dir / "file-coverage.json"
    command = ["bun", "tests/e2e/clients/web/quick_access_file_coverage.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised_command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                          "--timeout-seconds", "150", "--", *command]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(
                subprocess.run, supervised_command, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_COVERAGE_WORKSPACE_B": str(workspaces[1]),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=180, check=False,
            )
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
            "supervised_command": supervised_command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        assert receipt["singleOwnerNegativeCoverage"] is True
        assert receipt["smallQuotaAndContinuation"] is True
        assert receipt["coverageBindsCursor"] is True
        assert receipt["partitioned"] and receipt["filteredModel"] and receipt["onlyOpenEditors"]
        assert receipt["pageErrors"] == []
        logs = context.boxteam_home_for_node(request.node.nodeid) / "state/logs/backend"
        backend_logs = [log.read_text() for log in logs.glob("*.log")]
        owner_logs = {
            owner["workspace_id"]: [log for log in backend_logs
                                    if f"Workspace 挂载 begin: workspace_id={owner['workspace_id']}" in log]
            for owner in receipt["owners"]
        }
        assert all(len(entries) == 1 for entries in owner_logs.values())
        observed_dispatch = []
        for response in [*receipt["receipts"], receipt["nestedReceipt"]]:
            expected = list(dict.fromkeys(branch["branch"]["owner"]["workspace_id"] for branch in response["summary"]["branches"]))
            actual = [owner for owner, entries in owner_logs.items()
                      if any(f'request_id="{response["requestId"]}"' in line and '"POST /api/v1/search ' in line
                             for line in entries[0].splitlines())]
            observed_dispatch.append({"request_id": response["requestId"], "expected": expected, "actual": actual})
        (context.artifacts_dir / "backend-dispatch.json").write_text(json.dumps(observed_dispatch, indent=2) + "\n")
        assert all(entry["actual"] == entry["expected"] for entry in observed_dispatch), observed_dispatch
        assert (workspaces[0] / "00-dirty.txt").read_text() == "A disk coverageword\n"
        assert (workspaces[1] / "00-dirty.txt").read_text() == "B disk coverageword\n"
        for name, body in nested_files.items():
            assert (workspaces[0] / name).read_text(encoding="utf-8") == body
    finally:
        close_gateway_process(gateway)
