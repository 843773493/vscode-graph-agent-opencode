"""规范音视频 URI 经原隔离媒体编辑器打开、复制与恢复。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import wave
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
    prepared = os.getenv("BOXTEAM_SOURCE_WEB_ASSETS")
    assets = Path(prepared) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_media_uri_reopen_keeps_owner_and_original_isolated_webview(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    other = prepare_default_test_workspace(
        workspace_root=context.workspace_root / "media-B",
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True,
    )
    install_test_workspace_config(workspace_root=other, config_path=workspace / ".boxteam/workspace.jsonc",
                                  schema_path=context.project_root / "configs/workspace_schema.jsonc")
    for target, frames in ((workspace, 4000), (other, 8000)):
        with wave.open(str(target / "same-audio.wav"), "wb") as audio:
            audio.setparams((1, 2, 8000, frames, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * frames)
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "媒体重开验收需要 Chromium"
    output = context.artifacts_dir / "media-reopen.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "150", "--",
               "node", "tests/integration/clients/web/quick_access/media_reopen.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_MEDIA_OTHER_WORKSPACE": str(other),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=165, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(output.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert result["audioOwnersIsolated"] and result["audioRestored"] and result["audioReopened"]
    assert result["videoDecoded"] and result["videoReopened"] and result["videoRestored"]
    assert result["executions"] == [] and result["fileWrites"] == []
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "媒体打开、重开与恢复不得触发模型调用"
