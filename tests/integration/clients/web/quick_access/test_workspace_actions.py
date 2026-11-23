"""工作区动作和宿主条件模式经过真实 Gateway、原导航 writer 及实际服务。"""

from __future__ import annotations

import asyncio
import hashlib
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
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(autouse=True)
def metadata_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_WORKSPACE_ACTIONS_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with (assets / "out/main.js").open("rb") as main:
        fingerprint = hashlib.file_digest(main, "sha256").hexdigest()
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "assets": str(assets), "main_sha256": fingerprint, "prepared": bool(prepared),
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_workspace_discovery_scope_and_explicit_runtime_actions(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "工作区动作验收需要 Chromium"
    other = prepare_default_test_workspace(
        workspace_root=workspace.with_name("workspace-actions-B"),
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    )
    install_test_workspace_config(workspace_root=other, config_path=workspace / ".boxteam/workspace.jsonc",
                                  schema_path=context.project_root / "configs/workspace_schema.jsonc")
    result_path = context.artifacts_dir / "workspace-actions.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
               "node", "tests/integration/clients/web/quick_access/workspace_actions.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_ACTION_OTHER_WORKSPACE": str(other),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=135, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert result["browseOnly"] and result["scopeCancelRestored"] and result["scopeOnly"]
    assert result["startedWithoutActivation"] and result["activatedExplicitly"] and result["originalMenu"]
    assert result["forbiddenWrites"] == []
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "工作区控制面发现和动作不能触发 Agent 模型请求"


async def test_conditional_quick_access_modes_use_their_actual_services(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, _workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "原生条件模式验收需要 Chromium"
    result_path = context.artifacts_dir / "conditional-providers.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
               "node", "tests/integration/clients/web/quick_access/conditional_providers.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=135, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert result["unexpectedWrites"] == [] and result["externalRequests"] == []
    assert {mode["prefix"] for mode in result["modes"]} == {
        "term ", "task ", "debug ", "debug consoles ", "ext ", "ext install ", "mcpr ", "issue ",
    }
    assert result["pluginInstallationCancelled"] and result["browserIssueStatus"]
    assert len(result["initialization"]) == 3
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "浏览条件模式和取消安装不得调用模型"
