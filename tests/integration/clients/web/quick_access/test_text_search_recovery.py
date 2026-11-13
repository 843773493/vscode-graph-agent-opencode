"""真实 Gateway、原文件 owner 和正式 Workbench 的文本分支恢复。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@pytest.fixture
def recovery_assets(request: pytest.FixtureRequest) -> Path:
    prepared = os.getenv("BOXTEAM_TEXT_SEARCH_RECOVERY_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with (assets / "out/main.js").open("rb") as main:
        fingerprint = hashlib.file_digest(main, "sha256").hexdigest()
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "assets": str(assets), "main_sha256": fingerprint, "prepared": bool(prepared),
    }) + "\n", encoding="utf-8")
    return assets


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ("recovery", "identity"))
async def test_text_branch_failure_remote_cancel_and_explicit_retry(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    search_gateway_port: int,
    recovery_assets: Path,
    scenario: str,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "文本分支恢复验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = Path(integration_workspace_root_path)
    template = context.project_root / "tests/fixtures/workspaces/default_test_workspace"
    workspaces = [prepare_test_workspace(workspace_root=root / name, template_root=template, template_items=()) for name in ("A", "B")]
    for target in workspaces:
        install_test_workspace_config(workspace_root=target, config_path=Path(integration_workspace_config_path),
                                      schema_path=root / ".boxteam/workspace_schema.jsonc")
        (target / "recover.txt").write_text(f"textrecovery {target.name} original\n", encoding="utf-8")
        (target / "cancel.txt").write_text(f"textcancel {target.name} original\n", encoding="utf-8")
        (target / "identity.txt").write_text("".join(f"{query} {target.name} original\n"
            for query in ("identityretry", "identityeditor", "identityafter")), encoding="utf-8")
    gateway = start_gateway_process(workspace_root=workspaces[0], default_backend_url=None, port=search_gateway_port,
        asgi_app="tests.integration.clients.web.quick_access.query_gateway_fixture:app",
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(recovery_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
    output = context.artifacts_dir / f"text-{scenario}.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
               "node", "tests/integration/clients/web/quick_access/text_search_recovery.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_RECOVERY_WORKSPACE_B": str(workspaces[1]),
                     "BOXTEAM_TEXT_SEARCH_SCENARIO": scenario,
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium}, capture_output=True, timeout=135, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_bytes(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_bytes(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
        result = json.loads(output.read_text())
        assert result["phase"] == "complete" and result["pageErrors"] == []
        if scenario == "recovery":
            assert result["failedKeepsSibling"] and result["cancelledKeepsSibling"] and result["retryOnlyFailedBranch"]
            assert result["closedRetryAbortsRead"]
        else:
            assert result["identityRevokesCandidates"] and result["identityCancelsRetry"] and result["identityCancelsSearchEditor"]
            assert result["newQueryUsesFrozenEditorScope"] and result["reopenUsesNewIdentity"]
        assert result["modelCalls"] == []
        assert len(result["initialization"]) == 3
        assert all(item["requestId"] for item in result["initialization"])
        for target in workspaces:
            assert (target / "recover.txt").read_text() == f"textrecovery {target.name} original\n"
            assert (target / "cancel.txt").read_text() == f"textcancel {target.name} original\n"
            assert (target / "identity.txt").read_text() == "".join(f"{query} {target.name} original\n"
                for query in ("identityretry", "identityeditor", "identityafter"))
    finally:
        close_gateway_process(gateway)
