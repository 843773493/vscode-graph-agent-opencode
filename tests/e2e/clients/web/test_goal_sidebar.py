from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import e2e_port_block_for_file
from tests.support.processes.gateway import (
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture
def goal_sidebar_gateway(request: pytest.FixtureRequest):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    artifacts.mkdir()
    reset_gateway_persistent_state(workspace_root=context.workspace_root)
    prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",),
    )
    build = subprocess.run(
        ["bun", "run", "--cwd", "src/clients/web", "build"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    (artifacts / "build.log").write_text(build.stdout + build.stderr)
    assert build.returncode == 0, build.stdout + build.stderr
    gateway = start_gateway_process(
        workspace_root=context.workspace_root,
        default_backend_url=None,
        port=e2e_port_block_for_file(Path(request.node.path)).port(21),
        extra_env={"BOXTEAM_WEB_ASSETS": str(Path.cwd() / "src/clients/web/dist")},
    )
    try:
        yield gateway, artifacts
    finally:
        close_gateway_process(gateway)


def test_goal_sidebar_through_real_workbench(goal_sidebar_gateway):
    gateway, artifacts = goal_sidebar_gateway
    environment = {
        **os.environ,
        "BOXTEAM_E2E_BASE_URL": f"http://127.0.0.1:{gateway.port}",
        "BOXTEAM_E2E_ARTIFACTS": str(artifacts),
    }
    result = subprocess.run(
        ["node", "tests/e2e/clients/web/goal_sidebar.mjs"],
        env=environment, capture_output=True, text=True, timeout=90, check=False,
    )
    (artifacts / "browser.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
