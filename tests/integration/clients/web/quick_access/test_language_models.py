"""真实 Web 扩展宿主的 JSON 分析随业务模型更新，不跨工作区复用。"""

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


async def test_json_symbols_follow_model_version_and_workspace_identity(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "语言模型验收需要 Chromium"
    other = prepare_default_test_workspace(
        workspace_root=workspace.with_name("language-B"),
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    )
    install_test_workspace_config(workspace_root=other, config_path=workspace / ".boxteam/workspace.jsonc",
                                  schema_path=context.project_root / "configs/workspace_schema.jsonc")
    originals = ('{"alphaOwner":1}\n', '{"betaOwner":2}\n')
    for root, text in zip((workspace, other), originals, strict=True):
        (root / "same.json").write_text(text, encoding="utf-8")
    result_path = context.artifacts_dir / "language-models.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
               "bun", "tests/integration/clients/web/quick_access/language_models.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_LANGUAGE_OTHER_WORKSPACE": str(other),
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
    assert result["initial"] == [["alphaOwner"], ["betaOwner"]]
    assert result["edited"] == [["alphaAfter"], ["betaOwner"]]
    assert result["reopened"] == ["alphaOwner"]
    assert result["restoredLanguage"] == [["alphaOwner"], ["betaOwner"]]
    assert result["plainTextToken"] == "No grammar or semantic tokens available."
    assert len(result["initialTokens"]) == 2
    for token in [*result["initialTokens"], result["editedToken"], result["reopenedToken"], result["restoredToken"]]:
        assert "support.type.property-name.json" in token
    assert result["workerScripts"], "必须观察原扩展宿主／语言服务脚本加载"
    for root, text in zip((workspace, other), originals, strict=True):
        assert (root / "same.json").read_text(encoding="utf-8") == text
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "语言模型查询不能触发 Agent 模型请求"
