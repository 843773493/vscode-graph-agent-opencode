"""真实 Workbench JSONC 来源编辑、CAS 冲突和未知提交查询。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)


@pytest.mark.asyncio
async def test_settings_source_editor_preserves_owner_and_draft(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "设置来源 UI 验收需要真实 Chromium"
    workspace_root = Path(integration_workspace_root_path)
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.fspath))
    config_path = Path(integration_workspace_config_path)
    configuration = commentjson.loads(config_path.read_text(encoding="utf-8"))
    for provider in configuration["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "source-editor-unused-key"
    configuration.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{ports.port(20)}"
    config_path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    package_path = context.artifacts_dir / "source-settings-package.zip"
    manifest = {"manifest_version": 1, "extension_contract_version": 1, "package_id": "source.settings-probe",
        "version": "1.0.0", "name": "正式来源设置验证", "plugins": [{"id": "preference", "name": "来源参数",
            "instance_scope": "gateway", "requires": [{"extension_point": "configuration", "version": 1}],
            "contributes": {"configuration": {"type": "object", "additionalProperties": False,
                "properties": {"visible": {"type": "boolean", "default": False},
                    "password": {"type": "string", "writeOnly": True}}}}}]}
    with ZipFile(package_path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest, ensure_ascii=False))
    gateway = start_gateway_process(workspace_root=workspace_root, default_backend_url=None,
        port=ports.port(20), boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            health = client.get("/api/gateway/health").raise_for_status().json()["data"]
            workspace_list = client.get("/api/gateway/workspaces").raise_for_status().json()["data"]
            assert workspace_list["active_workspace"]
            assert workspace_list["active_workspace"]["gateway_id"] == health["gateway_id"]
        result_path = context.artifacts_dir / "settings-source-editor.json"
        environment = os.environ | {
            "BOXTEAM_SETTINGS_SOURCE_FIXTURE": json.dumps({"baseUrl": f"http://127.0.0.1:{gateway.port}",
                "artifacts": str(context.artifacts_dir), "resultPath": str(result_path), "packagePath": str(package_path)}),
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
        }
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            run = await asyncio.to_thread(subprocess.run, ["bun", "tests/integration/clients/web/settings_source_editor.mjs"],
                cwd=Path.cwd(), env=environment | temporary, capture_output=True, text=True, timeout=210, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(run.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(run.stderr, encoding="utf-8")
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"exit_code": run.returncode,
            "command": ["bun", "tests/integration/clients/web/settings_source_editor.mjs"]}, indent=2) + "\n", encoding="utf-8")
        assert run.returncode == 0, f"真实设置来源 UI 失败：\n{run.stdout}\n{run.stderr}\n{result_path}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["phase"] == "complete" and not result["pageErrors"]
        assert result["gateway_saved"] and result["workspace_saved"]
        assert result["conflict_kept_draft"] and result["unknown_not_replayed"]
        assert result["readonly"] and result["frozen_owner"]
        assert result["original_settings_form_saved"] and result["draft_restored_after_reload"]
        assert result["plugin_schema_saved_without_runtime"] and result["form_and_json_share_draft"]
        assert result["resource_browse_only"] and result["resource_open_and_save"] and result["resource_readonly"]
        assert result["configuration_uri_opened"]
    finally:
        close_gateway_process(gateway)
