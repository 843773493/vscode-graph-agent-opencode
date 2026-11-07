"""原设置编辑器经过真实 Gateway 保存目标 profile，JSON 和重开共用文件 owner。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import commentjson
import pytest

from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    InstallationCase,
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    installation_case as installation_case,  # noqa: PLC0414
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    management_runtime as management_runtime,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def settings_management_component(management_runtime, runtime_manifest_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    context, component, runtime = management_runtime
    manifest = context.runtime_root_for_node(request.node.nodeid) / "settings-runtime.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
    payload["extension_host"] = {
        "node_executable": str(runtime.executable), "component_root": str(component),
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_TEST_MCP_RUNTIME_MANIFEST", str(manifest))


async def test_settings_editor_saves_target_json_and_restores_same_profile(installation_case: InstallationCase) -> None:
    case = installation_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "设置编辑器验收需要真实 Chromium"
    workspace_config = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    result_path = case.context.artifacts_dir / "settings-editor-browser.json"
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
        "bun", "tests/integration/clients/web/settings_editor_target.mjs"]
    with chromium_temporary_environment(case.context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"]) / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=135, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_BROWSER_BASE_URL": str(case.client.base_url).rstrip("/"), "BOXTEAM_BROWSER_RESULT_PATH": str(result_path)})
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": result.returncode,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    browser = json.loads(result_path.read_text(encoding="utf-8"))
    assert browser["phase"] == "complete" and browser["pageErrors"] == []
    assert browser["hostChannels"] == [], "独立设置不需要扩展宿主"
    assert browser["writes"] and all("generation" not in item for item in browser["writes"])
    assert browser["jsonSavedAfterClosingSettingsUI"] and browser["jsonReturnedToTargetUI"] and browser["restoredTarget"]
    binding = browser["binding"]
    uri = urlsplit(binding["context"]["remote_settings"]["native_uri"])
    assert uri.scheme == "file" and not uri.netloc
    native = Path(url2pathname(uri.path)).resolve()
    assert native.is_relative_to(case.home.resolve())
    assert commentjson.loads(native.read_text(encoding="utf-8")) == {"http.proxy": "http://target.invalid:8080", "http.proxyStrictSSL": False}
    assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == workspace_config
    assert all(write["gateway_id"] == binding["gateway_id"] and write["backend_id"] == binding["backend_id"]
        and write["profile_id"] == binding["profile_id"] for write in browser["writes"])
