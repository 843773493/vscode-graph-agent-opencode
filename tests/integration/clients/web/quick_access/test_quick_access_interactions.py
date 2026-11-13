from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)

INTERACTION_ASSETS = Path(
    "out/tests/integration/clients/web/quick_access/resources/test_remote_gateway_uri/"
    "runtime/plugin-command-connections-20261113/dist"
)
INTERACTION_MAIN_SHA256 = "5c06458b6c7472a094e37957227d485a064ba22bcabf92f611cdd70075caefc2"


@pytest.mark.asyncio
async def test_default_quick_access_interactions(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    search_gateway_port: int,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("Quick Access 交互验收需要 Chromium")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    assets = (context.project_root / INTERACTION_ASSETS).resolve(strict=True)
    main_sha256 = hashlib.file_digest((assets / "out/main.js").open("rb"), "sha256").hexdigest()
    if main_sha256 != INTERACTION_MAIN_SHA256:
        pytest.fail(f"复用的 Workbench main.js 指纹不匹配：{main_sha256}")
    root = Path(integration_workspace_root_path)
    template = context.project_root / "tests/fixtures/workspaces/default_test_workspace"
    workspace = prepare_default_test_workspace(workspace_root=root / "quick-access-interactions", template_root=template)
    install_test_workspace_config(
        workspace_root=workspace,
        config_path=Path(integration_workspace_config_path),
        schema_path=root / ".boxteam/workspace_schema.jsonc",
    )
    for index in range(24):
        (workspace / f"qa-interaction-target-{index:02}.txt").write_text(
            f"Quick Access interaction result {index:02}\nsecond row {index:02}\n", encoding="utf-8",
        )
    (workspace / "qa-interaction-symbols.json").write_text(
        '{\n  "alpha": 1,\n  "beta": {"value": "preview target"}\n}\n', encoding="utf-8",
    )
    (workspace / "qa-interaction-origin.txt").write_text("origin first\norigin second\norigin third\n", encoding="utf-8")
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=None, port=search_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(assets)},
    )
    result_path = context.artifacts_dir / "quick-access-interactions.json"
    screenshot_path = context.artifacts_dir / "quick-access-interactions-failure.png"
    command = ["node", "tests/integration/clients/web/quick_access/default_interactions_page.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(
                subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary,
                     "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=420, check=False,
            )
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
            "command": command, "exit_code": completed.returncode, "assets": str(assets),
            "main_sha256": main_sha256,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text(encoding="utf-8"))
        assert receipt["initialFocusAndAccessibleList"] is True
        assert receipt["layeredResultsAndCommands"] is True
        assert receipt["keyboardBoundaries"] is True
        assert receipt["modifiedAcceptAndBackground"] is True
        assert receipt["escapeBackAndFocusLost"] is True
        assert receipt["previewCancelRestoresPosition"] is True
        assert receipt["compositionAndTheme"]["compositionEvents"] is True
        assert receipt["compositionAndTheme"]["themeClass"]
        assert receipt["compositionAndTheme"]["aria"]["active"]
        assert receipt["pageErrors"] == []
        assert receipt["inputMemoryAndActiveScroll"] is True
    finally:
        close_gateway_process(gateway)
