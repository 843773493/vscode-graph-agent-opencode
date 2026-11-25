"""真实附件上传、模型处理与 Web 目录编辑；模型边界复用 model_stream。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import commentjson
import httpx
import pytest

from app.core.resource_uri import file_uri_child, parse_resource_uri
from app.core.workspace_identity import read_workspace_id
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 完整 Workbench 产物
)
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    history_target as history_target,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session, with_message_run_operation_id
from tests.support.api_waiters import wait_for_job_done
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@pytest.fixture(autouse=True)
def attachment_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_SOURCE_WEB_ASSETS")
    assets = Path(prepared) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))

@pytest.fixture
async def attachment_case(history_target, request) -> AsyncIterator[tuple]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{history_target.gateway_port}",
                                 headers=LOCAL_TOKEN_HEADERS, timeout=60) as client:
        yield client, context, history_target.workspace, history_target.replay


@pytest.mark.asyncio
async def test_composer_uploads_before_send_and_restores_reference_after_reload(attachment_case, request) -> None:
    client, context, workspace, replay = attachment_case
    health = (await client.get("/api/gateway/health")).json()["data"]
    active = (await client.get("/api/gateway/workspaces")).json()["data"]["active_workspace"]
    client.headers["X-BoxTeam-Target-Gateway-Id"] = health["gateway_id"]
    client.headers["X-BoxTeam-Workspace-Id"] = active["workspace_id"]
    session = await create_session(client, "附件独立上传验收")
    thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
    config = commentjson.loads((workspace / ".boxteam/workspace.jsonc").read_text())
    provider = config["settings"]["workspace"]["llm"]["providers"]["primary"]
    model = provider["models"]["default"]["model"]
    endpoint = config["profiles"]["provider.primary"]["settings"]["model_options"]["endpoint"]
    replay.control.plan_model_calls(session, thread, cast(list[dict[str, object]], build_cassette(
        asset_id="composer-preupload", model=model, url=endpoint + "/chat/completions",
        frames=chat_completion_frames(response_id="chatcmpl-preupload", model=model,
            deltas=({"role": "assistant", "content": "附件已通过独立上传进入消息。"},), finish_reason="stop"),
    ).raw["interactions"]))
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "附件上传 Web 验收需要 Chromium"
    environment = {**os.environ, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
        "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
        "BOXTEAM_BROWSER_FIXTURE": json.dumps({"gatewayId": health["gateway_id"], "workspaceId": active["workspace_id"],
            "sessionId": session, "threadId": thread}),
        "BOXTEAM_BROWSER_RESULT_PATH": str(context.artifacts_dir / "preupload.json"),
        "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "preupload.png"),
    }
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        process = await asyncio.to_thread(subprocess.run, ["bun", "tests/integration/clients/web/attachment_uploads.mjs"],
            env={**environment, **temporary}, capture_output=True, text=True, timeout=100, check=False)
    (context.artifacts_dir / "preupload.stdout.raw").write_text(process.stdout)
    (context.artifacts_dir / "preupload.stderr.raw").write_text(process.stderr)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads((context.artifacts_dir / "preupload.json").read_text())
    assert result["restored"] and result["removed"] and result["sent"] and result["pageErrors"] == []
    await wait_for_job_done(client, result["jobId"])


@pytest.mark.asyncio
async def test_attachment_directory_shortcut_and_derived_edit_survive_web_reload(attachment_case, request) -> None:
    client, context, workspace, replay = attachment_case
    health = await client.get("/api/gateway/health")
    assert health.status_code == 200, health.text
    gateway_id = health.json()["data"]["gateway_id"]
    listing = await client.get("/api/gateway/workspaces")
    assert listing.status_code == 200, listing.text
    active = listing.json()["data"]["active_workspace"]
    workspace_id = active["workspace_id"]
    assert active == {"gateway_id": gateway_id, "workspace_id": read_workspace_id(workspace)}
    client.headers["X-BoxTeam-Target-Gateway-Id"] = gateway_id
    client.headers["X-BoxTeam-Workspace-Id"] = workspace_id
    session = await create_session(client, "附件目录浏览与编辑")
    main = await client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main.status_code == 200, main.text
    thread = main.json()["data"]["thread_id"]
    config = commentjson.loads((workspace / ".boxteam/workspace.jsonc").read_text())
    provider = config["settings"]["workspace"]["llm"]["providers"]["primary"]
    model = provider["models"]["default"]["model"]
    endpoint = config["profiles"]["provider.primary"]["settings"]["model_options"]["endpoint"]

    def plan(replies: list[dict[str, object]]) -> list[str]:
        return replay.control.plan_model_calls(session, thread, [cast(list[dict[str, object]], build_cassette(
            asset_id="attachment-directory-web", model=model,
            url=endpoint + "/chat/completions",
            frames=chat_completion_frames(
                response_id=f"chatcmpl-directory-{index}", model=model,
                deltas=({"role": "assistant", **reply},),
                finish_reason="tool_calls" if "tool_calls" in reply else "stop",
            ),
        ).raw["interactions"])[0] for index, reply in enumerate(replies)])

    original = "上传原件保持不变 Ω\n"
    calls = plan([{"content": "附件目录已收到。"}])
    sent = await client.post(f"/api/v1/sessions/{session}/messages", json=with_message_run_operation_id({
        "message": {"content": "读取目录说明，保留原件。", "attachments": [{
            "file_id": "inline:source", "name": "source.txt", "content_type": "text/plain",
            "data_url": "data:text/plain;base64," + base64.b64encode(original.encode()).decode(),
        }]}, "run": {"mode": "single_agent", "agent_id": "default"},
    }))
    assert sent.status_code == 200, sent.text
    await wait_for_job_done(client, sent.json()["data"]["job_id"])
    history = await client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
    assert history.status_code == 200, history.text
    attachment, = history.json()["data"]["items"][0]["user_messages"][0]["attachments"]
    derived = file_uri_child(attachment["directory_uri"], "derived", "result.txt")
    content = "模型生成的派生文件 Ω\n"
    edited = "浏览器修改后的派生文件 Ω\n"
    calls += plan([{"tool_calls": [{"index": 0, "id": "write-directory-result", "type": "function", "function": {
        "name": "write_file", "arguments": json.dumps({"file_path": derived, "content": content, "expected_version": "absent"}),
    }}]}, {"content": "派生文件已生成。"}])
    sent = await client.post(f"/api/v1/sessions/{session}/messages", json=with_message_run_operation_id({
        "message": {"content": "在附件目录的派生区写入处理结果。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    }))
    assert sent.status_code == 200, sent.text
    await wait_for_job_done(client, sent.json()["data"]["job_id"])
    wire = json.loads((replay.artifacts / (calls[0] + ".json")).read_text())["request"]
    manifest = json.dumps(wire, ensure_ascii=False)
    for value in (attachment["directory_uri"], attachment["file_id"], file_uri_child(attachment["directory_uri"], "derived"), "保留原件"):
        assert value in manifest
    assert str(workspace) not in manifest
    normalized = parse_resource_uri(attachment["directory_uri"], local_gateway_id=gateway_id).display_uri
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    assert chromium is not None, "附件目录 Web 验收需要 Chromium"
    result_path = context.artifacts_dir / "attachment-directory-browser.json"
    environment = {**os.environ,
        "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
        "BOXTEAM_BROWSER_FIXTURE": json.dumps({"gatewayId": gateway_id, "workspaceId": workspace_id, "sessionId": session, "threadId": thread,
            "directoryUri": normalized, "attachment": attachment, "content": content, "edited": edited}),
        "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
        "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "attachment-directory-browser.png"),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    temporary = context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp"
    with chromium_temporary_environment(temporary) as temporary_environment:
        process = await asyncio.to_thread(subprocess.run,
            ["bun", "tests/integration/clients/web/attachment_directory.mjs"],
            env={**environment, **temporary_environment}, capture_output=True, text=True, check=False, timeout=130,
        )
    (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout)
    (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr)
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"exit_code": process.returncode}) + "\n")
    assert process.returncode == 0, process.stdout + process.stderr
    proof = json.loads(result_path.read_text())
    assert proof["reopened"] and proof["saved"] and proof["viewRestored"]
    assert proof["pageErrors"] == proof["settingsFailures"] == []
    settings = await client.get(f"/api/v1/sessions/{session}/file-tree-settings")
    assert settings.status_code == 200, settings.text
    shortcut, = settings.json()["data"]["session_shortcuts"]
    assert shortcut["path"] == normalized
    assert settings.json()["data"]["default_shortcuts"] == []
    fresh = await create_session(client, "附件快捷路径不扩散")
    other = await client.get(f"/api/v1/sessions/{fresh}/file-tree-settings")
    assert other.status_code == 200 and other.json()["data"]["effective_shortcuts"] == [], other.text
    for uri, expected in ((attachment["file_id"], original), (derived, edited)):
        response = await client.get("/api/v1/workspace/files/content", params={"path": uri})
        assert response.status_code == 200 and response.json()["data"]["content"] == expected, response.text
