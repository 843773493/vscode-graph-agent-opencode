"""真实消息附件复制出的无扩展名 URI 沿原媒体编辑器重开及恢复。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.test_browser_view_sessions import (
    browser_backend as browser_backend,  # noqa: PLC0414
)
from tests.support.api_helpers import with_message_run_operation_id
from tests.support.api_waiters import wait_for_job_done
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_ports_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(autouse=True)
def metadata_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_SOURCE_WEB_ASSETS")
    assets = Path(prepared) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


@pytest.fixture
async def attachment_media_case(
    request: pytest.FixtureRequest, browser_backend: tuple[str, Path, ModelStreamReplay],
) -> AsyncIterator[tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay]]:
    backend_url, workspace, replay = browser_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_ports_for_file(Path(request.node.path))
    other = prepare_default_test_workspace(
        workspace_root=context.workspace_root / "attachment-B",
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True,
    )
    install_test_workspace_config(workspace_root=other, config_path=workspace / ".boxteam/workspace.jsonc",
                                  schema_path=context.project_root / "configs/workspace_schema.jsonc")
    # 两个真实 Backend 都显式绑定同一受控 ID port；Gateway 仍使用正式入口与目标路由。
    secondary = await asyncio.to_thread(start_backend_process, workspace_root=str(other), port=ports.port(1),
        log_name="attachment-media-B", asgi_app="tests.support.e2e_control.backend_app:app",
        boxteam_home=workspace.parent / "boxteam-home",
        env_overrides={**replay.environment, "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{ports.port(20)}"})
    gateway = None
    try:
        gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace, default_backend_url=backend_url,
                                          port=ports.port(20), artifacts_dir=context.artifacts_dir / "gateway")
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=90) as client:
            _data(await client.post("/api/gateway/workspaces/local", json={"root_path": str(other),
                "backend_url": f"http://127.0.0.1:{secondary.port}", "name": "Attachment B"}))
            yield client, context, workspace, replay
    finally:
        if gateway is not None:
            await asyncio.to_thread(close_gateway_process, gateway)
        await asyncio.to_thread(close_backend_process, secondary)


def _data(response: httpx.Response) -> dict:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["request_id"] == response.headers["X-Request-ID"]
    return payload["data"]


def _wav(frames: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 8000, frames, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * frames)
    return output.getvalue()


async def test_attachment_uri_reopen_decodes_original_media_and_preserves_owner(
    request: pytest.FixtureRequest,
    attachment_media_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, replay = attachment_media_case
    registry = _data(await client.get("/api/gateway/workspaces?check_health=false"))
    second = next(item for item in registry["items"] if item["name"] == "Attachment B")
    configuration = json.loads((workspace / ".boxteam/workspace.jsonc").read_text())
    primary = configuration["settings"]["workspace"]["llm"]["providers"]["primary"]["models"]["default"]
    endpoint = configuration["profiles"]["provider.primary"]["settings"]["model_options"]["endpoint"]
    owners = []
    for label, item, frames in [("A", registry["active_workspace"], 4000), ("B", second, 8000)]:
        owner = {"gatewayId": item["gateway_id"], "workspaceId": item["workspace_id"]}
        headers = {"X-BoxTeam-Target-Gateway-Id": owner["gatewayId"], "X-BoxTeam-Workspace-Id": owner["workspaceId"]}
        assert _data(await client.get("/api/v1/workspace", headers=headers))["workspace_id"] == owner["workspaceId"]
        session = _data(await client.post("/api/v1/sessions", headers=headers, json={"title": f"附件媒体 {label}"}))
        session_id = session["session_id"]
        thread_id = _data(await client.get(f"/api/v1/sessions/{session_id}/main-thread", headers=headers))["thread_id"]
        cassette = build_cassette(asset_id=f"attachment-media-{label.lower()}", model=primary["model"],
            url=endpoint.rstrip("/") + "/chat/completions", frames=chat_completion_frames(
                response_id=f"chatcmpl-attachment-media-{label}", model="e2e-stub-model",
                deltas=({"role": "assistant", "content": "已收到媒体附件。"},), finish_reason="stop"))
        planned = replay.control.plan_model_calls(session_id, thread_id, cast(list[dict[str, object]], cassette.raw["interactions"]))
        assert len(planned) == 1
        sources = [("same-audio.wav", "audio/wav", _wav(frames))]
        if label == "A":
            sources.extend([
                ("图片 原件 %.jpg", "image/jpeg", (workspace / "assets/test.jpg").read_bytes()),
                ("视频 原件.mp4", "video/mp4", (workspace / "assets/multimodal-test.mp4").read_bytes()),
            ])
        attachments = [{"file_id": f"inline:{name}", "name": name, "content_type": mime,
                        "data_url": f"data:{mime};base64," + base64.b64encode(content).decode("ascii")}
                       for name, mime, content in sources]
        accepted = _data(await client.post(f"/api/v1/sessions/{session_id}/threads/{thread_id}/messages", headers=headers,
            json=with_message_run_operation_id({"message": {"content": "请确认已收到媒体附件。", "attachments": attachments},
                  "run": {"mode": "single_agent", "agent_id": "default"}})))
        async with httpx.AsyncClient(base_url=client.base_url, headers={**client.headers, **headers}, timeout=90) as owned:
            await wait_for_job_done(owned, accepted["job_id"])
        page = _data(await client.get(f"/api/v1/sessions/{session_id}/threads/{thread_id}/attachments", headers=headers))
        assert len(page["items"]) == len(sources)
        assert all(item["file_id"].endswith("/original/content") for item in page["items"])
        owners.append({**owner, "label": label, "sessionId": session_id, "threadId": thread_id,
                       "attachments": page["items"]})
    assert owners[0]["workspaceId"] != owners[1]["workspaceId"]
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "附件媒体重开验收需要 Chromium"
    output = context.artifacts_dir / "attachment-media-reopen.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "210", "--",
               "node", "tests/integration/clients/web/quick_access/attachment_media_reopen.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_ATTACHMENT_MEDIA_FIXTURE": json.dumps(owners),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=225, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(output.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert all(result[key] for key in ["copiedWithoutRead", "imageDecoded", "imageRestored", "imageReopened",
                                      "audioOwnersIsolated", "audioRestored", "audioReopened",
                                      "videoDecoded", "videoRestored", "videoReopened"])
    assert result["executions"] == [] and result["fileWrites"] == []
    with replay.control._condition:
        calls = {key: count for key, count in replay.control._counters.items() if key.startswith("model_call/")}
    assert sum(calls.values()) == 2, calls
