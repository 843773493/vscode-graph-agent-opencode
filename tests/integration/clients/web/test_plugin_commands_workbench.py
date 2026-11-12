"""真实 Gateway 与正式 Workbench 的自有命令、菜单和快捷键纵向验收。"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

import commentjson
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.plugin_package import native_plugin_target
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)

ENTRY = '''
import sys, os, asyncio, json
sys.path.insert(0, os.environ['BOXTEAM_PROJECT_ROOT'])
from fastapi import FastAPI
import uvicorn
from app.core.logging_config import configure_application_logging
from app.core.trace_middleware import TraceMiddleware
from app.runtime.plugin_commands import plugin_command_router
configure_application_logging(service='command-ui-proof')
async def execute(arguments, context):
    await context.progress(1, 2, '真实插件进度')
    if arguments.get('wait'): await asyncio.Event().wait()
    with open(context.parameters['output'], 'a', encoding='utf-8') as log:
        log.write(json.dumps({'call_id': context.call.call_id, 'version': 'VERSION', 'request_id': context.request_id}) + '\\n')
    return {'value': arguments.get('value', 'ui-default'), 'pid': os.getpid(), 'version': 'VERSION'}
async def shutdown(): server.should_exit = True
app = FastAPI()
app.add_middleware(TraceMiddleware)
app.include_router(plugin_command_router({'execute': execute}, shutdown=shutdown))
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(os.environ['BOXTEAM_PLUGIN_PORT']), log_config=None))
asyncio.run(server.serve())
'''


@pytest.fixture
def plugin_commands_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    lease, probes = E2EProcessLease.acquire_available(
        context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", range(ports.port(20), ports.end + 1))
    try:
        (context.artifacts_dir / "port-allocation.json").write_text(json.dumps(probes), encoding="utf-8")
        yield lease.ports[0]
    finally:
        lease.release()


def test_workbench_plugin_commands_lifecycle(request: pytest.FixtureRequest, web_workbench_assets: Path,
                                             plugin_commands_port: int) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "自有命令 Workbench 验收需要 Chromium"
    workspace = prepare_test_workspace(workspace_root=context.workspace_root,
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    install_test_workspace_config(workspace_root=workspace, config_path=context.project_root / "configs/tests/workspace/default.jsonc",
        schema_path=context.project_root / "configs/workspace_schema.jsonc")
    configuration = workspace / ".boxteam/workspace.jsonc"
    config = commentjson.loads(configuration.read_text())
    for provider in config["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "command-ui-unused-key"
    configuration.write_text(json.dumps(config), encoding="utf-8")
    command = {"id": "execute", "handler": "execute", "title": "命令验收：执行", "category": "插件验收",
        "arguments_schema": {"type": "object", "additionalProperties": False,
            "properties": {"value": {"type": "string"}, "wait": {"type": "boolean"}}},
        "result_schema": {"type": "object", "required": ["value", "pid", "version"],
            "properties": {"value": {"type": "string"}, "pid": {"type": "integer"}, "version": {"type": "string"}}},
        "required_capabilities": ["read"]}
    manifest = {"package_id": "proof.command-ui", "version": "1.0.0", "name": "Command UI proof",
        "target": native_plugin_target("python"), "plugins": [{"id": "commands", "name": "Commands", "instance_scope": "gateway",
            "runtime": "python", "entrypoint": ["entry.py"], "management_protocol_version": 1,
            "requires": [{"extension_point": point, "version": 1} for point in ("commands", "menus", "keybindings", "configuration")],
            "contributes": {"commands": [command],
                "menus": [{"id": "editor-menu", "location": "editor/context", "command_id": "execute", "group": "navigation"}],
                "keybindings": [{"id": "execute-key", "command_id": "execute", "key": "ctrl+alt+shift+9", "mac": "cmd+alt+shift+9"}],
                "configuration": {"type": "object", "required": ["output"], "additionalProperties": False,
                    "properties": {"output": {"type": "string"}}}}}]}
    packages = {}
    for version, title, key in (("1.0.0", "命令验收：执行", "9"), ("2.0.0", "命令验收：新版执行", "8")):
        manifest["version"] = version
        contributions = manifest["plugins"][0]["contributes"]
        contributions["commands"][0]["title"] = title
        contributions["keybindings"][0].update(key=f"ctrl+alt+shift+{key}", mac=f"cmd+alt+shift+{key}")
        archive_path = context.artifacts_dir / f"command-ui-{version}.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("plugin.jsonc", json.dumps(manifest))
            archive.writestr("entry.py", ENTRY.replace("VERSION", version))
        packages[version] = str(archive_path.resolve())
    (workspace / "command-ui.txt").write_text("命令菜单验收\n", encoding="utf-8")
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=plugin_commands_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    try:
        node = shutil.which("node")
        assert node, "Workbench Playwright 驱动需要发行 Node"
        supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
        command_line = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "150", "--",
            node, "tests/integration/clients/web/plugin_commands_workbench.mjs"]
        output = context.artifacts_dir / "executions.txt"
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = subprocess.run(command_line, cwd=context.project_root, timeout=165, check=False, capture_output=True,
                env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                    "BOXTEAM_COMMAND_UI_INPUT": json.dumps({"baseUrl": f"http://127.0.0.1:{plugin_commands_port}",
                        "artifacts": str(context.artifacts_dir), "packagePaths": packages, "output": str(output.resolve())})})
        (context.artifacts_dir / "browser.stdout.raw").write_bytes(result.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_bytes(result.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command_line, "exit_code": result.returncode}), encoding="utf-8")
        assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")
        receipt = json.loads((context.artifacts_dir / "command-ui.json").read_text())
        assert receipt["phase"] == "complete" and receipt["pageErrors"] == [], receipt
        executions = [json.loads(line) for line in output.read_text().splitlines()]
        sample_count = receipt["workload"]["commandSamples"]
        assert sample_count == 20
        assert [item["version"] for item in executions] == ["1.0.0"] * (4 + sample_count) + ["2.0.0"] * 3, receipt
        assert len({item["call_id"] for item in executions}) == 7 + sample_count
        assert all(item["request_id"] for item in executions)
        trace = (context.artifacts_dir / "gateway/gateway.stderr.log").read_text(encoding="utf-8")
        durations = {request_id: float(seconds) * 1000 for seconds, request_id in re.findall(
            r"\[TRACE\] method=\S+ path=\S+ status=200 duration=([\d.]+)s request_id=(\S+)", trace)}
        operations = {"command": receipt["warmCommands"]}
        for operation in ("status", "settings"):
            operations[operation] = [sample for sample in receipt["warmReads"] if sample["operation"] == operation]
        summaries = {}
        for operation, samples in operations.items():
            assert len(samples) == sample_count
            for sample in samples:
                assert sample["requestId"] in durations, (operation, sample, "缺少原 Gateway Trace")
                sample["gatewayMilliseconds"] = durations[sample["requestId"]]
                # 沿客户端既有15秒完整请求预算，不另设每层独立等待期限。
                assert 0 < sample["milliseconds"] < 15_000, sample
            elapsed = sorted(sample["milliseconds"] for sample in samples)
            internal = sorted(sample["gatewayMilliseconds"] for sample in samples)
            summaries[operation] = {"samples": sample_count, "p95Ms": elapsed[math.ceil(sample_count * .95) - 1],
                "maxMs": elapsed[-1], "gatewayP95Ms": internal[math.ceil(sample_count * .95) - 1],
                "gatewayMaxMs": internal[-1]}
        (context.artifacts_dir / "warm-request-measurements.json").write_text(json.dumps({
            "workload": receipt["workload"], "summaries": summaries, "samples": operations,
            "boundary": "command包含Workbench命令入口至完整结果；reads为浏览器fetch至完整JSON；Gateway为原Trace中间件总耗时，含插件HTTP及日志，不称内部handler耗时",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert receipt["windowReopenedSameInstance"] and receipt["installedVersionKeptBinding"]
        assert receipt["uninstalledVersions"] == ["1.0.0", "2.0.0"]
        assert all(not psutil.pid_exists(pid) for pid in receipt["exitedPids"])
        assert not (context.boxteam_home_for_node(request.node.nodeid) / "plugins/packages/proof.command-ui").exists()
    finally:
        close_gateway_process(gateway)
