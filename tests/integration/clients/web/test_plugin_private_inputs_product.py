"""经真实 Gateway 与 Workbench 保存插件秘密，并验证插件重建后读取。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
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
from tests.support.plugin_package import native_plugin_target
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)

_PLUGIN_SOURCE = '''
import asyncio, hashlib, json, os, sys
sys.path.insert(0, os.environ['BOXTEAM_PROJECT_ROOT'])
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
from app.core.logging_config import configure_application_logging
from app.runtime.plugin_commands import plugin_command_router
configure_application_logging(service='private-input-product-probe')
identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])
print('plugin-instance-log ' + identity['plugin_id'] + ' ' + identity['instance_id'], flush=True)
parameters = json.loads(os.environ['BOXTEAM_PLUGIN_PARAMETERS'])
async def shutdown(): server.should_exit = True
app = FastAPI()
app.include_router(plugin_command_router({}, shutdown=shutdown))
@app.get('/services/echo/value')
async def echo(request: Request):
    value = parameters.get('credential', '')
    entries = parameters.get('entries', [])
    nested = entries[0].get('credential', '') if entries else ''
    return {'instance_id': identity['instance_id'], 'credential_digest': hashlib.sha256(value.encode()).hexdigest(),
        'nested_digest': hashlib.sha256(nested.encode()).hexdigest(), 'entry_labels': [entry['label'] for entry in entries],
        'credential_present': bool(value), 'request_id': request.headers.get('X-Request-ID')}
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(os.environ['BOXTEAM_PLUGIN_PORT']), log_config=None))
asyncio.run(server.serve())
'''


def _write_package(path: Path) -> None:
    manifest = {
        "manifest_version": 1,
        "package_id": "product.private-inputs",
        "version": "1.0.0",
        "name": "秘密输入产品验收",
        "target": native_plugin_target("python"),
        "extension_contract_version": 1,
        "plugins": [
            {
                "id": plugin_id,
                "name": label,
                "instance_scope": scope,
                "requires": [
                    {"extension_point": "services", "version": 1},
                    {"extension_point": "configuration", "version": 1},
                ],
                "contributes": {
                    "services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}],
                    "configuration": {
                        "type": "object",
                        "additionalProperties": False,
                        "$defs": {"entry": {"type": "object", "additionalProperties": False,
                            "properties": {"label": {"type": "string"}, "credential": {
                                "allOf": [{"type": "string", "minLength": 8, "writeOnly": True}],
                            }}}},
                        "properties": {
                            "credential": {"type": "string", "minLength": 8, "writeOnly": True},
                            "entries": {"type": "array", "items": {"$ref": "#/$defs/entry"}},
                        },
                    },
                },
                "runtime": "python",
                "entrypoint": ["nested/private_input_probe.py"],
                "management_protocol_version": 1,
            }
            for plugin_id, label, scope in (
                ("gateway-probe", "Gateway 私有输入探针", "gateway"),
                ("workspace-probe", "工作区私有输入探针", "workspace"),
            )
        ],
    }
    with ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("nested/private_input_probe.py", _PLUGIN_SOURCE)


@pytest.mark.asyncio
async def test_plugin_private_inputs_gateway_and_workspace_product_flow(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "插件秘密输入验收需要真实 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace_root = Path(integration_workspace_root_path)
    config_path = Path(integration_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    for provider in config["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "private-input-unused-key"
    ports = integration_port_block_for_file(Path(request.node.fspath))
    port = ports.port(20)
    config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{port}"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    package_path = context.artifacts_dir / "private-input-probe.zip"
    _write_package(package_path)
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url=None,
        port=port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)},
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30, trust_env=False) as client:
            health = client.get("/api/gateway/health").raise_for_status().json()["data"]
            workspaces = client.get("/api/gateway/workspaces").raise_for_status().json()["data"]
            assert workspaces["active_workspace"]
            assert workspaces["active_workspace"]["gateway_id"] == health["gateway_id"]
        result_path = context.artifacts_dir / "plugin-private-inputs-product.json"
        fixture = {
            "baseUrl": f"http://127.0.0.1:{gateway.port}",
            "packagePath": str(package_path),
            "resultPath": str(result_path),
        }
        environment = os.environ | {
            "BOXTEAM_PLUGIN_PRIVATE_INPUT_FIXTURE": json.dumps(fixture),
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
        }
        supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
        command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "240",
            "--", "bun", "tests/integration/clients/web/plugin_private_inputs_product.mjs"]
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            run = await asyncio.to_thread(subprocess.run, command, cwd=Path.cwd(), env=environment | temporary,
                capture_output=True, text=True, timeout=255, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(run.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(run.stderr, encoding="utf-8")
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
            "command": command, "exit_code": run.returncode, "supervisor_exit_code": run.returncode,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert run.returncode == 0, f"插件秘密输入 Workbench 验收失败：\n{run.stdout}\n{run.stderr}\n{result_path}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["phase"] == "complete" and result["gateway_rebuilt"] and result["workspace_rebuilt"]
        assert result["secret_values_absent"] and not result["pageErrors"]
        assert result["instance_logs_gateway"] and result["instance_logs_workspace"] and result["stopped_instance_logs"]
        assert result["structured_private_input"] and result["structured_private_cleared"]
        assert result["private_source_inheritance"]
        # 清除来源覆盖不等于擦除私有正文；原引用仍由各自 owner 保存。
        for reference in result["cleared_overlay"].values():
            private_file = context.boxteam_home_for_node(request.node.nodeid) / "credentials/plugins" / f"{reference['version']}.json"
            assert private_file.is_file()
    finally:
        close_gateway_process(gateway)
