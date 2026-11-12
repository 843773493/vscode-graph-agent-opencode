"""原文件 owner、Gateway 与 Quick Access／Search Editor 的普通诊断闭环。"""

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
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def diagnostic_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_SEARCH_DIAGNOSTIC_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with (assets / "out/main.js").open("rb") as main:
        fingerprint = hashlib.file_digest(main, "sha256").hexdigest()
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "assets": str(assets), "main_sha256": fingerprint, "prepared": bool(prepared),
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_plain_info_warning_and_limited_state_reach_original_search_ui(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    for name in ("a", "b"):
        (workspace / f"notice-{name}.txt").write_text("c" * 2200 + "\nnoticeword " + "p" * 2200 + "\n", encoding="utf-8")
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "搜索诊断验收需要 Chromium"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
               "node", "tests/integration/clients/web/quick_access/search_diagnostics.mjs"]
    result_path = context.artifacts_dir / "diagnostics.json"
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=135, check=False)
    (context.artifacts_dir / "driver.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "driver.json").write_text(json.dumps({"command": command, "exit_code": completed.returncode}) + "\n")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert result["quickInfo"] and result["quickLimited"] and result["editorWarning"]
    assert len(result["initialization"]) == 3
    assert all(item["requestId"] for item in result["initialization"])
    assert all((workspace / f"notice-{name}.txt").read_text().startswith("c" * 2200) for name in ("a", "b"))
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "文件搜索及诊断展示不得触发模型调用"
