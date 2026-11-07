"""原 Workbench 安装通知取消实际 uv 下载，不写配置或伪造发布。"""

# pytest 按名称注入复用 fixture，测试参数须与导入同名。
# ruff: noqa: F811

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator

import pytest

from tests.integration.gateway.test_mcp_artifact_installation import (  # noqa: F401 - 显式复用 pytest fixture，避免复制装具。
    artifact_index,
    installation_case,
    installation_route,
)
from tests.support.paths import chromium_temporary_environment


@pytest.fixture
def artifact_download_gate() -> Iterator[tuple[threading.Event, threading.Event]]:
    entered, release = threading.Event(), threading.Event()
    try:
        yield entered, release
    finally:
        release.set()


def test_original_install_notification_cancels_real_download(installation_case, artifact_index, artifact_download_gate):
    case = installation_case
    _, installation_base = installation_route(case.client)
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "MCP 安装取消需要真实 Chromium"
    original = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    command = ["bun", "tests/integration/clients/web/mcp_artifact_installation.mjs"]
    with chromium_temporary_environment(case.context.artifacts_dir / "chromium-tmp") as temporary:
        result = subprocess.run(command, timeout=150, check=False, capture_output=True, text=True, env={**os.environ, **temporary,
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable, "BOXTEAM_ARTIFACT_FIXTURE": json.dumps({
                "baseUrl": str(case.client.base_url).rstrip("/"), "registryUrl": artifact_index.url,
                "artifacts": str(case.context.artifacts_dir), "testFile": "tests/integration/clients/web/test_mcp_installation_cancel.py", "cancel": True})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2))
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((case.context.artifacts_dir / "browser-installation-result.json").read_text())
    assert observed["phase"] == "complete" and observed["pageErrors"] == []
    assert observed["operation"]["status"] == "cancelled" and observed["operation"]["artifact"] is None
    assert artifact_download_gate[0].is_set()
    assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == original
    assert case.client.get(installation_base + "/installations").json()["data"] == []
    assert case.client.get("/api/v1/mcp/servers").json()["data"] == []
