"""产品文件读取失败产生日志，经正式 Gateway／Workbench 搜索和定位。"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.schemas.search_federation import FederatedSearchRequest
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


async def test_runtime_logs_use_process_sources_and_original_diagnostics(
    request: pytest.FixtureRequest, integration_workspace_root_path: str, integration_workspace_config_path: str,
    search_gateway_port: int, web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "运行日志正文验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = Path(integration_workspace_root_path)
    workspaces = [prepare_test_workspace(workspace_root=root / name,
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", template_items=()) for name in ("A", "B")]
    for workspace in workspaces:
        install_test_workspace_config(workspace_root=workspace, config_path=Path(integration_workspace_config_path),
            schema_path=root / ".boxteam/workspace_schema.jsonc")
    artifacts = context.artifacts_dir / "gateway"
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspaces[0], default_backend_url=None,
        port=search_gateway_port, boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=artifacts,
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true",
                   "BOXTEAM_SERVICE_LOG_PATH": str(artifacts / "gateway.stderr.log")})
    result_path = context.artifacts_dir / "runtime-log-content.json"
    command = ["bun", "tests/integration/clients/web/quick_access/runtime_log_content.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_LOG_WORKSPACE_B": str(workspaces[1]),
                     "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "runtime-log-content-failure.png"),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium}, capture_output=True, text=True, timeout=180, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        for key in ("unselectedHasNoQuery", "workspaceSourcesIsolated", "gatewaySourceSeparate", "samePaneNewLocation",
                    "exactRecordAfterAppend", "staleReferenceRejected", "noFullDiagnosticsRead", "onlyFederatedSearch"):
            assert receipt[key], key
        assert receipt["pageErrors"] == []
        diagnostic = receipt["cursorDiagnostic"]
        client = GatewaySessionContextClient(gateway_url=f"http://127.0.0.1:{gateway.port}",
            local_token_supplier=lambda: LOCAL_TOKEN_HEADERS["X-Local-Token"])
        try:
            events = [event.model_dump(mode="json") async for event in client.resource_search.search(
                FederatedSearchRequest.model_validate(diagnostic), request_id="runtime-log-stale-cursor")]
            branch, = events[-1]["summary"]["branches"]
            assert branch["state"] == "failed" and branch["code"] == "stale_runtime_log_cursor"
            assert branch["error_owner"] == "runtime_log" and branch["reason"] == "file_changed"
            assert branch["cursor"] == diagnostic["continuations"][0]["cursor"]
            assert not any(event["type"] == "page" for event in events)
            (context.artifacts_dir / "cursor-federation.json").write_text(json.dumps(events, ensure_ascii=False, indent=2) + "\n")
        finally:
            await client.aclose()
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
