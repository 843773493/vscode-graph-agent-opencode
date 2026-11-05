"""独立 Workbench 经真实 Gateway 展示资源身份拒绝、纠正与历史恢复。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.services.business.session_context_resource import (
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 复用正式工作台构建。
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    browser_backend as browser_backend,  # noqa: PLC0414 - request 指向本文件，资源独立隔离。
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)


async def test_rejected_identity_and_correction_survive_workbench_refresh(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, ModelStreamReplay],
    web_workbench_assets: Path,
) -> None:
    backend_url, workspace, replay = browser_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium") or shutil.which("google-chrome")
    assert chromium, "Workbench 工具拒绝验收需要 Chromium"
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=backend_url, port=ports.port(20),
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)},
    )
    try:
        base_url = f"http://127.0.0.1:{gateway.port}"
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            guest = await client.post("/api/gateway/users/guest", json={"tracking": {"source": "resource-identity-tool-loop"}})
            assert guest.status_code == 200, guest.text
            workspaces = await client.get("/api/gateway/workspaces")
            assert workspaces.status_code == 200, workspaces.text
            active_owner = workspaces.json()["data"]["active_workspace"]
            client.headers["X-BoxTeam-Target-Gateway-Id"] = active_owner["gateway_id"]
            client.headers["X-BoxTeam-Workspace-Id"] = active_owner["workspace_id"]
            created = await client.post("/api/v1/sessions", json={"title": "资源身份纠正验收", "agent_id": "default"})
            assert created.status_code == 200, created.text
            session = created.json()["data"]
            assert session["workspace_id"] == active_owner["workspace_id"]
            session_id = session["session_id"]
            selected = await client.patch(f"/api/v1/sessions/{session_id}", json={"provider_id": "primary"})
            assert selected.status_code == 200 and selected.json()["data"]["current_provider_id"] == "primary"
            main = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert main.status_code == 200, main.text
            thread_id = main.json()["data"]["thread_id"]
            owner = {"gatewayId": active_owner["gateway_id"], "workspaceId": session["workspace_id"], "sessionId": session_id, "threadId": thread_id}
            valid = {"resource_identity": str(workspace_session_inventory_identity(session["workspace_id"])),
                     "uri": workspace_session_inventory_uri(session["workspace_id"]), "scope": "workspace", "view": "inventory"}
            locations = ["/private/web-identity", r"C:\private\web-identity", r"\\target\private\web-identity"]

            def tool_delta(identities: list[str], prefix: str) -> dict[str, object]:
                return {"role": "assistant", "tool_calls": [
                    {"index": index, "id": f"{prefix}-{index}", "type": "function", "function": {
                        "name": "invoke_extension_tool", "arguments": json.dumps({
                            "tool_name": "read_context", "arguments": {**valid, "resource_identity": identity},
                        }, ensure_ascii=False),
                    }} for index, identity in enumerate(identities)
                ]}

            final = "工作台资源身份已纠正，读取完成。"
            responses = [tool_delta(locations, "rejected"), tool_delta([valid["resource_identity"]], "corrected"),
                         {"role": "assistant", "content": final}]
            interactions = [build_cassette(
                asset_id="workbench-resource-identity", model="browser-chat-tool-loop-stub",
                url=f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
                frames=chat_completion_frames(
                    response_id=f"chatcmpl-resource-identity-{index}", model="browser-chat-tool-loop-stub",
                    deltas=[response], finish_reason="stop" if index == 2 else "tool_calls",
                ),
            ).raw["interactions"] for index, response in enumerate(responses)]
            interactions = [cast(list[dict[str, object]], entries)[0] for entries in interactions]
            calls = replay.control.plan_model_calls(session_id, thread_id, interactions)
            result_path = context.artifacts_dir / "browser-result.json"
            environment = {**os.environ, **replay.environment, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                           "BOXTEAM_BROWSER_BASE_URL": base_url, "BOXTEAM_BROWSER_OWNER": json.dumps(owner),
                           "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_BROWSER_FINAL": final,
                           "BOXTEAM_BROWSER_MODEL_CALL_IDS": json.dumps(calls),
                           "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "failure.png")}
            command = ["bun", "tests/integration/clients/web/resource_identity_tool_loop.mjs"]
            with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
                result = await asyncio.to_thread(subprocess.run, command, env={**environment, **temporary},
                                                capture_output=True, text=True, timeout=90, check=False)
            (context.artifacts_dir / "browser.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
                "command": command, "exit_code": result.returncode,
            }, indent=2) + "\n", encoding="utf-8")
            assert result.returncode == 0, f"Workbench 验收失败：{context.artifacts_dir}\n{result.stdout}\n{result.stderr}"
            actual = json.loads(result_path.read_text())
            assert actual["owner"] == owner and actual["live"] == actual["restored"]
            assert (actual["live"]["failed"], actual["live"]["completed"]) == (6, 1)
            assert len(actual["live"]["partIds"]) == len(set(actual["live"]["partIds"])) == 8
            assert actual["live"]["results"] == [
                "ResourceIdentity resource_identity 不得携带路径/URI/credential 形态",
            ] * 3
            assert actual["pageErrors"] == []
            job = await client.get(f"/api/v1/jobs/{actual['jobId']}")
            assert job.status_code == 200, job.text
            assert (job.json()["data"]["status"], job.json()["data"]["session_id"], job.json()["data"]["thread_id"]) == (
                "completed", session_id, thread_id,
            )
            recorded = [json.loads((replay.artifacts / f"{call}.json").read_text()) for call in calls]
            assert [entry["identity"] for entry in recorded] == [
                {"session_id": session_id, "thread_id": thread_id, "model_call_id": call} for call in calls
            ]
            tools = [message for message in recorded[-1]["request"]["messages"] if message["role"] == "tool"]
            assert [message["tool_call_id"] for message in tools] == ["rejected-0", "rejected-1", "rejected-2", "corrected-0"]
            assert [message["content"] for message in tools[:3]] == actual["live"]["results"]
            assert json.loads(tools[-1]["content"])["resource_identity"] == valid["resource_identity"]
            assert "private" not in json.dumps(tools, ensure_ascii=False)
            with replay.control._condition:
                assert replay.control._counters[f"model_call/{session_id}/{thread_id}"] == 3
    finally:
        close_gateway_process(gateway)
