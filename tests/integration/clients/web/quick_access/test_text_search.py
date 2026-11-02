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
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture
def search_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = integration_port_block_for_file(Path(request.node.path)).port(20)
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", (port,))
    lease.acquire()
    try:
        yield port
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_original_quick_text_search_and_view_keep_business_scope(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    search_gateway_port: int,
    web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("快速文本搜索验收需要 Chromium")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspaces = Path(integration_workspace_root_path)
    template = context.project_root / "tests/fixtures/workspaces/default_test_workspace"
    workspace, other, replacement = [
        prepare_default_test_workspace(workspace_root=workspaces / name, template_root=template)
        for name in ("A", "B", "C")
    ]
    # B/C 是全新工作区，稳定身份由正式注册／启动入口生成。
    (other / ".boxteam/workspace-identity.json").unlink()
    (replacement / ".boxteam/workspace-identity.json").unlink()
    for target in (workspace, other, replacement):
        install_test_workspace_config(
            workspace_root=target, config_path=Path(integration_workspace_config_path),
            schema_path=workspaces / ".boxteam/workspace_schema.jsonc",
        )
    (workspace / "quick-scope.txt").write_text("diskword quickscope\n", encoding="utf-8")
    (workspace / "watch-contract.txt").write_text("watch initial\n", encoding="utf-8")
    (other / "quick-scope.txt").write_text("foreign-disk quickscope\n", encoding="utf-8")
    (replacement / "quick-scope.txt").write_text("replacement-C quickscope\n", encoding="utf-8")
    (workspace / "context-lines.txt").write_text("A context before\ncontextbranchonly\ncontextbranchonly\nA context after", encoding="utf-8")
    (other / "context-lines.txt").write_text("B context before\ncontextbranchonly\nB context after", encoding="utf-8")
    (workspace / ".gitignore").write_text("ignored-search.txt\n", encoding="utf-8")
    (workspace / "ignored-search.txt").write_text("hidden ignorescopeonly\n", encoding="utf-8")
    (workspace / "allowed-search.txt").write_text("visible ignorescopeonly\n", encoding="utf-8")
    result_path = context.artifacts_dir / "search-result.json"
    screenshot_path = context.artifacts_dir / "search-failure.png"
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=None, port=search_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
    )
    try:
        command = ["bun", "tests/integration/clients/web/quick_access/text_search.mjs"]
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(
                subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                     "BOXTEAM_SEARCH_OTHER_WORKSPACE": str(other),
                     "BOXTEAM_SEARCH_WORKSPACE": str(workspace),
                     "BOXTEAM_SEARCH_REPLACEMENT_WORKSPACE": str(replacement),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=210, check=False,
            )
        (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}) + "\n")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        assert receipt["dirtyModelSearch"] is True
        assert receipt["viewTransferAndRerun"] is True
        assert receipt["movedViewRerun"] is True
        assert receipt["crossWorkspaceIsolation"] is True
        assert receipt["reverseOwnerIsolation"] is True
        assert receipt["onlyOpenIsolation"] is True
        assert receipt["defaultIgnoreRules"] is True
        assert receipt["disabledIgnoreRules"] is True
        assert receipt["editorViewScope"] is True
        assert receipt["editorSplitScopes"] is True
        assert receipt["editorSavedScope"] is True
        assert receipt["editorReloadScopes"] is True
        assert receipt["editorDefinitionOwner"] is True
        assert receipt["editorLegacyScope"] is True
        assert receipt["editorInvalidScope"] is True
        assert receipt["editorDirtyContext"] is True
        assert receipt["editorAutomaticContextRerun"] is True
        assert receipt["editorDeepBackupRecovery"] is True
        assert receipt["fileWatch"]["idleContentReads"] == 0
        assert receipt["fileWatch"]["externalUpdate"] is True
        assert receipt["fileWatch"]["dirtyConflict"] is True
        assert receipt["editorLatePaneIsolation"] is True
        assert receipt["editorRouteRecovery"]["original"] != receipt["editorRouteRecovery"]["current"]
        assert receipt["pageErrors"] == []
        assert (workspace / "quick-scope.txt").read_text(encoding="utf-8") == "diskword quickscope\n"
        assert (other / "quick-scope.txt").read_text(encoding="utf-8") == "replacement-C quickscope\n"
        assert (other.with_name(other.name + "-relocated") / "quick-scope.txt").read_text(encoding="utf-8") == "foreign-disk quickscope\n"
    finally:
        close_gateway_process(gateway)
