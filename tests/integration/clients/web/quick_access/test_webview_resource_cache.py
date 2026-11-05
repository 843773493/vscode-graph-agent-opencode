"""原 Markdown Webview 经真实文件 owner 加载 CSS／字体及复用缓存。"""

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
    metadata_data,
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def metadata_web_assets(monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


@pytest.mark.asyncio
async def test_original_markdown_webview_revalidates_css_and_font(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "原 Webview 资源验收需要 Chromium"
    current = await metadata_data(client, "/api/gateway/workspaces")
    owner = current["active_workspace"]
    client.headers.update({"X-BoxTeam-Target-Gateway-Id": owner["gateway_id"],
                           "X-BoxTeam-Workspace-Id": owner["workspace_id"]})
    roots = await metadata_data(client, "/api/v1/workspace/files/roots")
    directory = workspace / "webview-cache"
    directory.mkdir()
    (directory / "preview.md").write_text(
        '# 原 Webview 缓存\n\n<link rel="stylesheet" href="./style.css">\n\n'
        '<link rel="stylesheet" href="./retained.css">\n\n'
        '<p class="cache-probe">原文件 CSS 与字体</p>\n', encoding="utf-8",
    )
    (directory / "retained.css").write_text(
        '.cache-probe { background-color: rgb(90, 91, 92); }\n', encoding="utf-8",
    )
    (directory / "style.css").write_text(
        '@font-face { font-family: CacheProbe; src: url("./font.ttf"); }\n'
        '.cache-probe { font-family: CacheProbe; color: rgb(17, 34, 51); }\n', encoding="utf-8",
    )
    shutil.copyfile(context.project_root / "src/clients/workbench/node_modules/@vscode/codicons/dist/codicon.ttf",
                    directory / "font.ttf")
    runtime = context.runtime_root_for_node(request.node.nodeid)
    result_path = context.artifacts_dir / "webview-cache.json"
    command = ["bun", "tests/integration/clients/web/quick_access/webview_resource_cache.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    protected = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                 "--timeout-seconds", "150", "--", *command]
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        process = await asyncio.to_thread(subprocess.run, protected, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                 "BOXTEAM_WEBVIEW_FIXTURE": json.dumps({"gatewayId": owner["gateway_id"],
                     "workspaceId": owner["workspace_id"], "rootUri": roots["workspace_uri"],
                     "directory": str(directory)}),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=165, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": protected, "exit_code": process.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert len(result["eviction"]["removed"]) == len(result["eviction"]["restored"]) == 2
    assert any("/webview-cache/retained.css" in entry["url"] for entry in result["eviction"]["retained"])
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "原文件 Webview 读取不应触发模型请求"
