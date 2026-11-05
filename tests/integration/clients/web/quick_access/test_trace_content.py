"""执行与 fork 入口生成真实 Trace，经正式 Gateway 工作台查询及精确定位。"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from app.schemas.search_federation import FederatedSearchRequest
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    browser_backend as browser_backend,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)

QUERY = "轨迹定位😀词"


async def test_trace_content_opens_unloaded_child_and_reuses_original_event(
    request: pytest.FixtureRequest, browser_backend: tuple[str, Path, ModelStreamReplay],
    web_workbench_assets: Path, search_gateway_port: int,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Trace 正文验收需要 Chromium"
    backend_url, workspace, replay = browser_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace,
        default_backend_url=backend_url, port=search_gateway_port, artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    model = "browser-chat-tool-loop-stub"
    endpoint = integration_port_block_for_file(Path(request.node.path)).port(10)
    texts = ["准备 fork", "开头😀\n" + "a" * 12000 + QUERY + "\n第二处" + QUERY + "\n结束", "后续执行已完成"]
    interactions = [cast(list[object], build_cassette(asset_id="trace-content", model=model,
        url=f"http://127.0.0.1:{endpoint}/v1/chat/completions", frames=chat_completion_frames(
            response_id=f"trace-content-{index}", model=model,
            deltas=[{"role": "assistant", "content": text}], finish_reason="stop")).raw["interactions"])[0]
        for index, text in enumerate(texts)]
    result_path = context.artifacts_dir / "trace-content.json"
    command = ["bun", "tests/integration/clients/web/quick_access/trace_content.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, **replay.environment,
                     "BOXTEAM_TRACE_SEARCH_INTERACTIONS": json.dumps(interactions), "BOXTEAM_TRACE_SEARCH_QUERY": QUERY,
                     "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "trace-content-failure.png"),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=180, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        for key in ("unselectedHasNoContentQuery", "unopenedChildLocated", "samePaneNewLocation", "exactRecordAfterAppend",
                    "staleReferenceRejected", "noFullTraceRead", "onlySelectedOwnerAndCategory"):
            assert receipt[key], key
        assert receipt["pageErrors"] == []
        diagnostic = receipt["cursorDiagnostics"]
        transport = GatewaySessionContextClient(gateway_url=f"http://127.0.0.1:{gateway.port}",
            local_token_supplier=lambda: LOCAL_TOKEN_HEADERS["X-Local-Token"])
        try:
            original = [event async for event in transport.resource_search.search(FederatedSearchRequest.model_validate({
                "scope": {"mode": "origin", "origin": diagnostic["owner"]}, "queries": [diagnostic["query"]],
            }), request_id="trace-scope")]
            assert original[0].scope is not None and original[-1].summary is not None
            branch, = original[-1].summary.branches
            events = [event.model_dump(mode="json") async for event in transport.resource_search.search(
                FederatedSearchRequest.model_validate({"scope": {"mode": "resume", "scope": original[0].scope.scope.model_dump(mode="json")},
                    "queries": [diagnostic["query"]], "continuations": [{"branch": branch.branch.model_dump(mode="json"),
                        "cursor": diagnostic["cursor"]}]}), request_id="trace-stale-cursor")]
            failure, = events[-1]["summary"]["branches"]
            assert failure["state"] == "failed" and failure["code"] == "stale_trace_search_cursor"
            assert failure["retryable"] is True
            assert failure["error_owner"] == "trace_event_store" and failure["reason"] == "file_changed"
            assert failure["http_status"] == 409 and failure["cursor"] == diagnostic["cursor"]
            assert failure["returned_results"] == 0 and not any(event["type"] == "page" for event in events)
            (context.artifacts_dir / "cursor-federation.json").write_text(json.dumps(events, ensure_ascii=False, indent=2) + "\n")
        finally:
            await transport.aclose()
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
