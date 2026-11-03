"""真实 Workbench 模型覆盖经 Gateway 到原文件 owner，不传草稿正文。"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_text_search import (
    search_gateway_port as search_gateway_port,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


async def test_dirty_model_coverage_reaches_target_before_page_allocation(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    integration_workspace_config_path: str, search_gateway_port: int, web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "文件模型覆盖验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = Path(integration_workspace_root_path)
    targets = [prepare_default_test_workspace(
        workspace_root=root / name, template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    ) for name in ("A", "B", "C")]
    for target in targets:
        install_test_workspace_config(workspace_root=target, config_path=Path(integration_workspace_config_path),
                                      schema_path=root / ".boxteam/workspace_schema.jsonc")
        (target / "a.coverage.txt").write_text("coverageword " * 20, encoding="utf-8")
    (targets[0] / "b.coverage.txt").write_text("A remaining coverageword", encoding="utf-8")
    result_path = context.artifacts_dir / "model-coverage.json"
    gateway = await asyncio.to_thread(start_gateway_process,
        workspace_root=targets[0], default_backend_url=None, port=search_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
    command = ["bun", "tests/integration/clients/web/quick_access/file_model_coverage.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            process = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "model-coverage-failure.png"),
                     "BOXTEAM_COVERAGE_OTHER": str(targets[1]), "BOXTEAM_COVERAGE_STOPPED": str(targets[2]),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=150, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": process.returncode}) + "\n")
        assert process.returncode == 0, f"{process.stdout}\n{process.stderr}\n{result_path}"
        result = json.loads(result_path.read_text())
        assert result["modelCoverage"] and result["smallPage"] and result["samePathDifferentOwner"]
        assert result["staleCoverageCursor"] and result["stoppedUnqueried"]
        assert result["pageErrors"] == []
        # 搜索从未替用户保存草稿。
        assert targets[0].joinpath("a.coverage.txt").read_text() == "coverageword " * 20
        assert targets[1].joinpath("a.coverage.txt").read_text() == "coverageword " * 20
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
