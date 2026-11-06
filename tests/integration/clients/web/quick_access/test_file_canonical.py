"""真实文件树、公开引用、联邦候选及原热退出恢复共用规范文件身份。"""

from __future__ import annotations

import asyncio
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
    prepare_default_test_workspace,
)


@pytest.fixture
def canonical_workspaces(request: pytest.FixtureRequest, integration_workspace_root_path: str,
                         integration_workspace_config_path: str) -> tuple[TestRunContext, list[Path]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = Path(integration_workspace_root_path)
    workspaces = [prepare_default_test_workspace(
        workspace_root=root / name, template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    ) for name in ("A", "B")]
    for index, workspace in enumerate(workspaces):
        install_test_workspace_config(workspace_root=workspace, config_path=Path(integration_workspace_config_path),
                                      schema_path=context.project_root / "configs/workspace_schema.jsonc")
        directory = workspace / "Mixed"
        directory.mkdir()
        directory.joinpath("Doc.json").write_text('{"diskOwner":1}\n' if index == 0 else '{"otherOwner":2}\n', encoding="utf-8")
    sensitive = workspaces[0] / "Mixed/Sensitive"
    sensitive.mkdir()
    if os.name == "nt":
        command = ["fsutil.exe", "file", "setCaseSensitiveInfo", str(sensitive), "enable"]
        completed = subprocess.run(command, capture_output=True, timeout=15, check=False)
        assert completed.returncode == 0, (command, completed.returncode, completed.stdout, completed.stderr)
    sensitive.joinpath("Case.json").write_text('{"caseUpper":1}\n', encoding="utf-8")
    sensitive.joinpath("case.json").write_text('{"caseLower":2}\n', encoding="utf-8")
    assert sorted(item.name for item in sensitive.iterdir()) == ["Case.json", "case.json"]
    workspaces[0].joinpath("Alias").symlink_to(workspaces[0] / "Mixed", target_is_directory=True)
    return context, workspaces


async def test_file_identity_and_dirty_backup_recovery_through_real_gateway(
    request: pytest.FixtureRequest, canonical_workspaces: tuple[TestRunContext, list[Path]],
    search_gateway_port: int, web_workbench_assets: Path,
) -> None:
    context, workspaces = canonical_workspaces
    result_path = context.artifacts_dir / "file-canonical.json"
    gateway = await asyncio.to_thread(start_gateway_process,
        workspace_root=workspaces[0], default_backend_url=None, port=search_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
               "bun", "tests/integration/clients/web/quick_access/file_canonical.mjs"]
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    browser_environment = {"PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium} if chromium else {}
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, **browser_environment,
                     "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "file-canonical-failure.png"),
                     "BOXTEAM_CANONICAL_WORKSPACE": str(workspaces[0]), "BOXTEAM_CANONICAL_OTHER": str(workspaces[1])},
                capture_output=True, text=True, timeout=195, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
            "command": command, "exit_code": completed.returncode,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["phase"] == "complete" and result["pageErrors"] == []
        assert result["threeEntriesOneDirtyModel"] and result["directResolverUsesDirtyModel"]
        assert result["completeOwnerIsolation"] and result["logicalLinkIndependent"] and result["mixedSensitivityIndependent"]
        assert result["normalReloadSaved"] and result["restoredConflictPreserved"]
        assert result["caseOnlyRename"] == (os.name == "nt")
        filename = "mIXED/DOC.json" if os.name == "nt" else "Mixed/Doc.json"
        assert workspaces[0].joinpath(filename).read_text(encoding="utf-8") == '{"savedBase":2}\n'
        assert workspaces[1].joinpath("Mixed/Doc.json").read_text(encoding="utf-8") == '{"otherOwner":2}\n'
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
