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
def catalog_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = e2e_port_block_for_file(Path(request.node.path)).port(20)
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", (port,))
    lease.acquire()
    try:
        yield port
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_quick_access_catalog_targets_use_original_navigation_and_exact_thread(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    catalog_gateway_port: int,
    web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("目录快速访问验收需要 Chromium")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(e2e_workspace_root_path)
    for index in range(55):
        (workspace / f"pagingfiles-{index:03d}.txt").write_text(f"page fixture {index}\n", encoding="utf-8")
    other = prepare_test_workspace(
        workspace_root=workspace / "other", template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
    install_test_workspace_config(workspace_root=other, config_path=Path(e2e_workspace_config_path),
                                  schema_path=workspace / ".boxteam/workspace_schema.jsonc")
    (workspace / "uri@target.txt").write_text("URI link A\n", encoding="utf-8")
    (other / "uri@target.txt").write_text("URI link B\n", encoding="utf-8")
    result_path = context.artifacts_dir / "catalog-targets.json"
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=None, port=catalog_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
    )
    try:
        command = ["bun", "tests/e2e/clients/web/quick_access_catalog_targets.mjs"]
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(
                subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_CATALOG_OTHER_WORKSPACE": str(other),
                     "BOXTEAM_CATALOG_WORKSPACE": str(workspace),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=120, check=False,
            )
        (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}) + "\n")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        assert receipt["sessionOpensMain"] is True
        assert receipt["unopenedChildKeepsIdentity"] is True
        assert receipt["folderUsesFreshNavigation"] is True
        assert receipt["deletedFolderRejected"] is True
        assert receipt["folderHasNoThreadOrExecution"] is True
        assert receipt["pagination"]["explicitIndependentPages"] is True
        assert receipt["pagination"]["selectionPreserved"] is True
        assert receipt["pagination"]["stalePageCleared"] is True
        assert receipt["pagination"]["explicitBranchRestart"] is True
        assert receipt["resourceLinks"]["childKeepsIdentity"] is True
        assert receipt["resourceLinks"]["sessionUsesMainAndSideGroup"] is True
        assert receipt["resourceLinks"]["fileUsesOriginalProjection"] is True
        assert receipt["resourceLinks"]["invalidUriIsNotRewritten"] is True
        assert receipt["resourceLinks"]["noExecution"] is True
        assert receipt["pageErrors"] == []
    finally:
        close_gateway_process(gateway)
