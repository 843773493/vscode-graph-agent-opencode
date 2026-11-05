"""真实资源引用与选定正文只加入明确 Thread 的原草稿，不触发执行。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(autouse=True)
def metadata_web_assets(monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


async def test_resource_actions_only_change_selected_thread_draft(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Composer资源动作验收需要Chromium"
    (workspace / "thread-source-only.txt").write_text("未打开的文件正文不应被读取", encoding="utf-8")
    (workspace / "thread-source-other.txt").write_text("第二个未打开文件也不应读取正文", encoding="utf-8")
    other = prepare_default_test_workspace(
        workspace_root=workspace.with_name("composer-source-B"),
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    )
    install_test_workspace_config(workspace_root=other, config_path=workspace / ".boxteam/workspace.jsonc",
                                  schema_path=context.project_root / "configs/workspace_schema.jsonc")
    (other / "selection.txt").write_text("原选定正文😀\n下一行", encoding="utf-8")
    result_path = context.artifacts_dir / "composer-resource-actions.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "150", "--",
               "bun", "tests/integration/clients/web/quick_access/composer_resource_actions.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_COMPOSER_OTHER_WORKSPACE": str(other),
                 "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "failure.png"),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=165, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    for key in ("cancelUnchanged", "referenceRecipient", "sourcePickerCancelUnchanged", "unopenedSourceReference",
                "multiSourceFailureNoAppend", "multiSourceKeptRecipient", "originatingThreadKeptRecipient",
                "pasteRecipient", "restoredDrafts", "sourceChangePreservesPaste"):
        assert result[key], key
    assert result["executions"] == []
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "引用与粘贴草稿不得触发模型调用"
