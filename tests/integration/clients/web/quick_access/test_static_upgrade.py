"""正式 Workbench 产物的隔离副本，在真实浏览器缓存保留时升级。"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    return str(prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",),
    ))


async def test_cached_workbench_executes_upgraded_bundle_and_worker(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    web_workbench_assets: Path, search_gateway_port: int,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "静态升级验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    assets = runtime / "mutable-build"
    shutil.copytree(web_workbench_assets, assets)
    # 仅副本加入观测标记；原构建、产品源码和其它测试不受影响。
    markers = {
        "out/main.js": ';globalThis.__boxteamStaticUpgrade="version-1";',
        "out/vs/editor/common/services/editorWebWorkerMain.js":
            ';if(self.name==="boxteam-static-upgrade")self.postMessage({boxteamStaticUpgrade:"version-1"});',
    }
    for path, marker in markers.items():
        with (assets / path).open("a", encoding="utf-8") as file:
            file.write("\n" + marker + "\n")
    gateway = await asyncio.to_thread(start_gateway_process,
        workspace_root=Path(integration_workspace_root_path), default_backend_url=None, port=search_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
    command = ["node", "tests/integration/clients/web/quick_access/static_upgrade.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                  "--timeout-seconds", "120", "--", *command]
    result_path = context.artifacts_dir / "static-upgrade.json"
    try:
        with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
            process = await asyncio.to_thread(subprocess.run, supervised, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_STATIC_UPGRADE_ASSETS": str(assets),
                     "BOXTEAM_BROWSER_RUNTIME_PATH": str(runtime),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=150, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
            "command": command, "supervised_command": supervised, "exit_code": process.returncode}) + "\n")
        assert process.returncode == 0, f"{process.stdout}\n{process.stderr}\n{result_path}"
        result = json.loads(result_path.read_text())
        assert result["warmRevalidation"] and result["upgradeExecuted"] and result["missingWorkerRejected"]
        assert result["pageErrors"] == []
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
