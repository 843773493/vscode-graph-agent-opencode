from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@pytest.fixture
def query_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = integration_port_block_for_file(Path(request.node.path)).port(20)
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", (port,))
    lease.acquire()
    try:
        yield port
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_query_branches_cancel_expire_and_retry_only_their_selected_owner(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    query_gateway_port: int,
    web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("查询生命周期验收需要 Chromium")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = Path(integration_workspace_root_path)
    template = context.project_root / "tests/fixtures/workspaces/default_test_workspace"
    workspaces = [prepare_test_workspace(workspace_root=root / name, template_root=template, template_items=())
                  for name in ("A", "B", "C")]
    for target in workspaces:
        install_test_workspace_config(workspace_root=target, config_path=Path(integration_workspace_config_path),
                                      schema_path=root / ".boxteam/workspace_schema.jsonc")
        (target / "branchscope-file.txt").write_text(target.name + " query lifecycle\n", encoding="utf-8")
    gateway = start_gateway_process(
        workspace_root=workspaces[0], default_backend_url=None, port=query_gateway_port,
        asgi_app="tests.integration.clients.web.quick_access.query_gateway_fixture:app",
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
    )
    result_path = context.artifacts_dir / "query-lifecycle.json"
    command = ["bun", "tests/integration/clients/web/quick_access/query_lifecycle.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(
                subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_QUERY_WORKSPACE_B": str(workspaces[1]),
                     "BOXTEAM_QUERY_WORKSPACE_C": str(workspaces[2]), "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=150, check=False,
            )
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        assert receipt["onlySelectedOwners"] is True
        assert receipt["serverBudget"] is True
        assert receipt["cancelledQueueAndLateResponses"] is True
        assert receipt["timeoutKeepsOtherBranches"] is True
        assert receipt["allFailuresRemainFailures"] is True
        assert receipt["retryOnlyFailedBranch"] is True
        assert receipt["offlineHasNoBusinessQuery"] is True
        assert receipt["identityRevokesCandidates"] is True
        assert receipt["pageErrors"] == []
        assert receipt["forbiddenWrites"] == []
    finally:
        close_gateway_process(gateway)
