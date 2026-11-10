"""真实 Workbench 双工作区的版本化 schema、来源保存及解绑。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_service_plugin_workspaces import (
    workspace_plugin_case as workspace_plugin_case,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(scope="module", autouse=True)
def settings_workbench(web_workbench_assets: Path):
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))
        yield


@pytest.mark.asyncio
async def test_saved_bindings_refresh_only_their_settings_schema(
    request: pytest.FixtureRequest,
    workspace_plugin_case,
) -> None:
    client, context, workspaces = workspace_plugin_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which(
        "chromium"
    )
    assert chromium, "设置 schema 验收需要真实 Chromium"
    packages = []
    for version, properties in [
        (
            "1.0.0",
            {
                "mode": {"type": "string", "default": "first"},
                "onlyOne": {"type": "boolean", "default": False},
            },
        ),
        (
            "2.0.0",
            {
                "mode": {"type": "boolean", "default": True},
                "onlyTwo": {"type": "number", "default": 2},
            },
        ),
        (
            "3.0.0",
            {
                "mode": {"type": "string", "default": "third"},
                "onlyThree": {"type": "number", "default": 3},
            },
        ),
    ]:
        path = context.artifacts_dir / f"schema-probe-{version}.zip"
        manifest = {
            "manifest_version": 1,
            "extension_contract_version": 1,
            "package_id": "schema.probe",
            "version": version,
            "name": "版本化来源参数",
            "plugins": [
                {
                    "id": "preference",
                    "name": "工作区参数",
                    "instance_scope": "workspace",
                    "requires": [{"extension_point": "configuration", "version": 1}],
                    "contributes": {
                        "configuration": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": properties,
                        }
                    },
                }
            ],
        }
        with ZipFile(path, "w") as archive:
            archive.writestr("plugin.jsonc", json.dumps(manifest, ensure_ascii=False))
        packages.append(str(path))
    result_path = context.artifacts_dir / "settings-schema-versions.json"
    environment = os.environ | {
        "BOXTEAM_SETTINGS_SCHEMA_FIXTURE": json.dumps(
            {
                "baseUrl": str(client.base_url).rstrip("/"),
                "workspacePaths": [str(path) for path in workspaces],
                "packagePaths": packages,
                "artifacts": str(context.artifacts_dir),
                "resultPath": str(result_path),
            }
        ),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    command = ["bun", "tests/integration/clients/web/settings_schema_versions.mjs"]
    with chromium_temporary_environment(
        context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp"
    ) as temporary:
        run = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=Path.cwd(),
            env=environment | temporary,
            capture_output=True,
            text=True,
            timeout=210,
            check=False,
        )
    (context.artifacts_dir / "browser.stdout.raw").write_text(
        run.stdout, encoding="utf-8"
    )
    (context.artifacts_dir / "browser.stderr.raw").write_text(
        run.stderr, encoding="utf-8"
    )
    (context.artifacts_dir / "browser-process.json").write_text(
        json.dumps({"command": command, "exit_code": run.returncode}, indent=2) + "\n",
        encoding="utf-8",
    )
    assert run.returncode == 0, (
        f"真实版本化设置 UI 失败：\n{run.stdout}\n{run.stderr}\n{result_path}"
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert result["isolated_versions"] and result["same_input_refreshed"]
    assert result["late_installed_schema_refreshed"] and result["ordinary_save_zero_schema_requests"]
    assert result["cross_client_install"]
    assert (
        result["unbind_released_only_owner"]
        and result["residue_preserved"]
        and result["zero_runtime"]
    )
