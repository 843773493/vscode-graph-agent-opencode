"""会话管理经过真实 Gateway、Workbench 原命令及目录 outbox。"""

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
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.terminal_plugin import copy_prepared_developer_tools_package


@pytest.fixture(autouse=True)
def session_action_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_SOURCE_WEB_ASSETS")
    assets = Path(prepared) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_session_management_uses_original_dialog_confirmation_and_outbox(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
    prepared_developer_tools_package: Path,
) -> None:
    client, context, _, replay = metadata_case
    copy_prepared_developer_tools_package(
        prepared_developer_tools_package,
        context.boxteam_home_for_node(request.node.nodeid),
    )
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "会话管理验收需要 Chromium"
    result_path = context.artifacts_dir / "session-actions.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
               "node", "tests/integration/clients/web/quick_access/session_actions.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=195, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert all(result[name] for name in ["unopenedRename", "originalMenu", "selectionCancel", "deleteCancel", "deleted"])
    assert result["forbiddenWrites"] == []
    with replay.control._condition:
        calls = {key: count for key, count in replay.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "查找、重命名及删除会话不能启动模型执行"
