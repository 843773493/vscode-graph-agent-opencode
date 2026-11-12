"""运行资源快速管理经过正式 Workbench、Gateway 和真实资源 owner。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(autouse=True)
def runtime_web_assets(monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


async def test_manage_runtime_resources_without_opening_details(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
    prepared_developer_tools_package: Path,
) -> None:
    client, context, workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "运行资源管理验收需要 Chromium"
    other = prepare_default_test_workspace(
        workspace_root=workspace.with_name("runtime-B"),
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True,
    )
    first = prepare_default_test_workspace(
        workspace_root=workspace.with_name("runtime-A"),
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True,
    )
    for root in (first, other):
        config_path = install_test_workspace_config(workspace_root=root,
            config_path=workspace / ".boxteam/workspace.jsonc",
            schema_path=context.project_root / "configs/workspace_schema.jsonc")
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})[
            "url"
        ] = str(client.base_url).rstrip("/")
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result_path = context.artifacts_dir / "runtime-actions.json"
    copy_prepared_developer_tools_package(
        prepared_developer_tools_package, context.boxteam_home_for_node(request.node.nodeid),
    )
    gateway_id = (await client.get("/api/gateway/health")).raise_for_status().json()["data"]["gateway_id"]
    managed_targets = []
    for root, name in ((first, "Runtime A"), (other, "Runtime B")):
        managed = (await client.post("/api/gateway/managed-workspaces", json={
            "root_path": str(root), "name": name, "create_directory": False,
        })).raise_for_status().json()["data"]
        managed_targets.append(next(item for item in managed["items"] if item["name"] == name))
    a, b = managed_targets
    await enable_developer_tools_plugins(
        client, gateway_id=gateway_id, workspace_id=a["workspace_id"], plugin_ids=("terminal", "browser"),
    )
    await enable_developer_tools_plugins(
        client, gateway_id=gateway_id, workspace_id=b["workspace_id"], plugin_ids=("terminal", "browser"),
    )
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
               "node", "tests/integration/clients/web/quick_access/resources/runtime_actions.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                 "BOXTEAM_SOURCE_WORKSPACE_A_ID": a["workspace_id"],
                 "BOXTEAM_SOURCE_OTHER_WORKSPACE_ID": b["workspace_id"],
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=195, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert all(result[key] for key in ("browseOnly", "cancelOnly", "directStop", "detachedWithoutDelete",
                                      "deletedSeparately", "originalReact", "revisionConflict", "ownerB",
                                      "focusOtherWorkspace", "focusExactTerminal", "focusBrowserWithoutAttach"))
    assert result["forbiddenWrites"] == []
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "资源管理不得触发模型调用"
