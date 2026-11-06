"""由原安装入口生成 profile，验证真实 Web 宿主与安装记录保护。"""

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


def _supervised(context: TestRunContext, arguments: list[str], seconds: int) -> list[str]:
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    return [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", str(seconds), "--", *arguments]


@pytest.fixture(autouse=True)
def metadata_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    assets = context.runtime_root_for_node(request.node.nodeid) / "extension-dist"
    arguments = ["node", "tests/integration/clients/web/quick_access/web_extensions.mjs", "--prepare-assets", str(web_workbench_assets), str(assets)]
    command = _supervised(context, arguments, 60)
    process = subprocess.run(command, cwd=context.project_root, capture_output=True, text=True, timeout=75, check=False)
    (context.artifacts_dir / "asset-preparation.stdout.raw").write_text(process.stdout, encoding="utf-8")
    (context.artifacts_dir / "asset-preparation.stderr.raw").write_text(process.stderr, encoding="utf-8")
    (context.artifacts_dir / "asset-preparation.json").write_text(json.dumps({
        "command": command, "exit_code": process.returncode,
        "boundary": "正式Workbench独占副本，仅添加明确测试扩展；不修改原bundle或预写安装记录",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert process.returncode == 0, process.stdout + process.stderr
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_real_web_extension_install_upgrade_restart_and_protected_profile(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, _workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Web扩展真实验收需要Chromium"
    runtime = context.runtime_root_for_node(request.node.nodeid)
    output = context.artifacts_dir / "web-extensions.json"
    command = _supervised(context, ["node", "tests/integration/clients/web/quick_access/web_extensions.mjs"], 180)
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        process = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_BROWSER_RUNTIME_PATH": str(runtime),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=195, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": process.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(output.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert [item["version"] for item in result["executions"]] == ["1.0.0", "1.0.0", "2.0.0"]
    assert result["declarativeTheme"] == {"installed": True, "color": "#123456", "restored": True}
    assert result["nodeOnlyRejected"] and result["corruptProfilePreserved"] and result["restoredProfile"] and result["uninstalled"]
    with provider.control._condition:
        calls = {key: value for key, value in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "Web扩展验收不能发起Agent模型请求"
