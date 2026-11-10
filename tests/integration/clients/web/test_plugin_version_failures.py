"""真实管理页面切版失败后保留期望，只由显式重启重新应用。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZipFile

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
    prepare_default_test_workspace,
)

_ENTRY = '''
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ['BOXTEAM_PROJECT_ROOT'])
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
from app.core.logging_config import configure_application_logging
from app.core.trace_middleware import TraceMiddleware
from app.runtime.plugin_commands import plugin_command_router
configure_application_logging(service='version-failure-probe')
identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])
attempts = Path(sys.argv[1])
with attempts.open('a', encoding='utf-8') as stream:
    stream.write(json.dumps({'version': identity['package_version'], 'pid': os.getpid()}) + '\\n')
# 受控故障只发生一次；记录在包外，安装原件保持只读。
if identity['package_version'] == '2.0.0' and sum(
    json.loads(line)['version'] == '2.0.0' for line in attempts.read_text().splitlines()) == 1:
    sys.exit(23)
async def shutdown(): server.should_exit = True
app = FastAPI()
app.add_middleware(TraceMiddleware)
@app.middleware('http')
async def fail_shutdown(request, call_next):
    if identity['package_version'] == '3.0.0' and request.url.path == '/internal/plugin/shutdown':
        return JSONResponse({'detail': 'controlled shutdown failure'}, status_code=500)
    return await call_next(request)
app.include_router(plugin_command_router({}, shutdown=shutdown))
@app.get('/services/echo/value')
async def echo(request: Request):
    return {'version': identity['package_version'], 'instance_id': identity['instance_id'],
        'request_id': request.headers.get('X-Request-ID')}
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(os.environ['BOXTEAM_PLUGIN_PORT']), log_config=None))
asyncio.run(server.serve())
'''


@pytest.fixture
def version_failure_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    lease, probes = E2EProcessLease.acquire_available(
        context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", range(ports.port(20), ports.end + 1))
    (context.artifacts_dir / "port-allocation.json").write_text(json.dumps(probes), encoding="utf-8")
    try:
        yield lease.ports[0]
    finally:
        lease.release()


def test_workbench_version_failure_keeps_desired_and_requires_explicit_restart(
    request: pytest.FixtureRequest, web_workbench_assets: Path, version_failure_port: int,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "插件换版失败页面验收需要真实 Chromium"
    workspace = prepare_default_test_workspace(workspace_root=context.workspace_root,
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        shared_skill_root=context.project_root / "resources/skills", fresh_identity=True)
    config_path = install_test_workspace_config(workspace_root=workspace,
        config_path=context.project_root / "configs/tests/workspace/default.jsonc",
        schema_path=context.project_root / "configs/workspace_schema.jsonc")
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    for provider in config["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "version-failure-unused-key"
    config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{version_failure_port}"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    attempts = context.artifacts_dir / "actual-plugin-starts.jsonl"
    packages = []
    for version in ("1.0.0", "2.0.0", "3.0.0"):
        path = context.artifacts_dir / f"version-failure-{version}.zip"
        manifest = {"package_id": "proof.version-failures", "version": version, "name": "换版失败探针包",
            "target": native_plugin_target("python"), "plugins": [{"id": "probe", "name": "运行探针",
                "instance_scope": "gateway", "runtime": "python", "entrypoint": ["entry.py", str(attempts)],
                "management_protocol_version": 1, "requires": [{"extension_point": "services", "version": 1}],
                "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]}}]}
        with ZipFile(path, "w") as archive:
            archive.writestr("plugin.jsonc", json.dumps(manifest, ensure_ascii=False))
            archive.writestr("entry.py", _ENTRY)
        packages.append(str(path))
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=version_failure_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    result_path = context.artifacts_dir / "version-failures.json"
    try:
        environment = os.environ | {"PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
            "BOXTEAM_PLUGIN_VERSION_FAILURE_FIXTURE": json.dumps({"baseUrl": f"http://127.0.0.1:{gateway.port}",
                "packages": packages, "attempts": str(attempts), "resultPath": str(result_path)})}
        supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
        command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
            "bun", "tests/integration/clients/web/plugin_version_failures.mjs"]
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            run = subprocess.run(command, cwd=context.project_root, env=environment | temporary,
                capture_output=True, text=True, timeout=195, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(run.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(run.stderr, encoding="utf-8")
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
            "exit_code": run.returncode}), encoding="utf-8")
        assert run.returncode == 0, f"换版失败页面验收失败：{run.stdout}\n{run.stderr}\n{result_path}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["phase"] == "complete" and not result["pageErrors"] and not result["externalRequests"]
        assert result["startFailure"] and result["stopFailure"] and result["explicitRecovery"]
        # 实际退出核验在管理回执之后、fixture强制回收之前；不把回收当产品验收。
        for process_id in result["exitedPids"]:
            assert not psutil.pid_exists(process_id), f"产品回执后原插件进程仍存活: {process_id}"
    finally:
        close_gateway_process(gateway)
