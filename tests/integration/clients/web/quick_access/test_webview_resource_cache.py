"""原 Markdown Webview 经真实文件 owner 加载 CSS／字体及复用缓存。"""

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
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_data,
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
def metadata_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_WEBVIEW_CACHE_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with (assets / "out/main.js").open("rb") as main:
        fingerprint = hashlib.file_digest(main, "sha256").hexdigest()
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "assets": str(assets), "main_sha256": fingerprint, "prepared": bool(prepared),
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def prepare_webview_files(
    client: httpx.AsyncClient, context: TestRunContext, workspace: Path,
) -> dict[str, str]:
    target = prepare_default_test_workspace(
        workspace_root=workspace.with_name("webview-cache-target"),
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    )
    install_test_workspace_config(
        workspace_root=target, config_path=workspace / ".boxteam/workspace.jsonc",
        schema_path=context.project_root / "configs/workspace_schema.jsonc",
    )
    registered = await client.post("/api/gateway/managed-workspaces", json={
        "root_path": str(target), "name": "Webview Cache Target",
    })
    assert registered.status_code == 200, registered.text
    assert registered.headers["X-Request-ID"] == registered.json()["request_id"]
    current = await metadata_data(client, "/api/gateway/workspaces")
    owner = next(item for item in current["items"] if item["name"] == "Webview Cache Target")
    assert owner["workspace_id"] != current["active_workspace"]["workspace_id"]
    client.headers.update({"X-BoxTeam-Target-Gateway-Id": owner["gateway_id"],
                           "X-BoxTeam-Workspace-Id": owner["workspace_id"]})
    roots = await metadata_data(client, "/api/v1/workspace/files/roots")
    directory = target / "webview-cache"
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
    return {"gatewayId": owner["gateway_id"], "workspaceId": owner["workspace_id"],
            "rootUri": roots["workspace_uri"], "directory": str(directory)}


def assert_webview_result(result_path: Path) -> dict:
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert len(result["eviction"]["removed"]) == len(result["eviction"]["restored"]) == 2
    assert any("/webview-cache/retained.css" in entry["url"] for entry in result["eviction"]["retained"])
    assert result["restart"]["before"] == result["restart"]["after"]
    deletion = result["deletion"]
    assert deletion["cached"] == deletion["cachedAfterFailure"]
    assert deletion["rawReadsBefore"] == deletion["rawReadsAfter"]
    assert deletion["rejection"]["status"] == 404
    assert deletion["restored"]["etag"] != deletion["cached"]["etag"]
    disconnection = result["disconnection"]
    assert disconnection["rejection"]["status"] == 503
    assert disconnection["cachedBefore"] == disconnection["cachedDuring"] == disconnection["cachedAfter"]
    assert disconnection["rawReadsBefore"] == disconnection["rawReadsWhileDisconnected"] == disconnection["rawReadsAfterReconnect"]
    return result


@pytest.mark.asyncio
async def test_original_markdown_webview_revalidates_css_and_font(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "原 Webview 资源验收需要 Chromium"
    fixture = await prepare_webview_files(client, context, workspace)
    runtime = context.runtime_root_for_node(request.node.nodeid)
    result_path = context.artifacts_dir / "webview-cache.json"
    command = ["node", "tests/integration/clients/web/quick_access/webview_resource_cache.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    protected = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                 "--timeout-seconds", "150", "--", *command]
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        process = await asyncio.to_thread(subprocess.run, protected, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                 "BOXTEAM_WEBVIEW_FIXTURE": json.dumps(fixture),
                 "BOXTEAM_WEBVIEW_PROFILE": str(runtime / "browser-profile"),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=165, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": protected, "exit_code": process.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert process.returncode == 0, process.stdout + process.stderr
    assert_webview_result(result_path)
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "原文件 Webview 读取不应触发模型请求"
