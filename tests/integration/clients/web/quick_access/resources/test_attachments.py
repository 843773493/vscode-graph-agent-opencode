"""真实 Thread 附件 Quick Access 的 Gateway 与 Workbench 页面验收。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import commentjson
import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    HistoryTarget,
)
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    history_target as history_target,  # noqa: PLC0414
)
from tests.support.api_waiters import wait_for_job_done
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@pytest.fixture
async def attachment_browser_case(
    request: pytest.FixtureRequest,
    history_target: HistoryTarget,
) -> AsyncIterator[tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{history_target.gateway_port}",
        headers=LOCAL_TOKEN_HEADERS,
        timeout=90,
    ) as client:
        yield client, context, history_target.workspace, history_target.replay


@pytest.fixture(autouse=True)
def attachment_web_assets(monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


def _api_data(response: httpx.Response) -> dict[str, object]:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["X-Request-ID"] == payload["request_id"]
    return cast(dict[str, object], payload["data"])


def _inline_attachment(name: str, content_type: str, content: bytes) -> dict[str, str]:
    encoded = base64.b64encode(content).decode("ascii")
    return {
        "file_id": f"inline:{name}",
        "name": name,
        "content_type": content_type,
        "data_url": f"data:{content_type};base64,{encoded}",
    }


def _completion(model: str, url: str) -> list[dict[str, object]]:
    cassette = build_cassette(
        asset_id="thread-attachment-quick-access",
        model=model,
        url=url,
        frames=chat_completion_frames(
            response_id="chatcmpl-thread-attachment-quick-access",
            model=model,
            deltas=({"role": "assistant", "content": "已收到附件。"},),
            finish_reason="stop",
        ),
    )
    return cast(list[dict[str, object]], cassette.raw["interactions"])


async def test_thread_attachment_quick_access_paginates_and_opens_original_preview(
    request: pytest.FixtureRequest,
    attachment_browser_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, replay = attachment_browser_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    assert chromium, "Thread 附件 Quick Access 页面验收需要 Chromium"

    health = _api_data(await client.get("/api/gateway/health"))
    workspaces = _api_data(await client.get("/api/gateway/workspaces?check_health=false"))
    owner = cast(dict[str, str], workspaces["active_workspace"])
    gateway_id = cast(str, health["gateway_id"])
    workspace_id = owner["workspace_id"]
    assert owner["gateway_id"] == gateway_id
    client.headers.update({
        "X-BoxTeam-Target-Gateway-Id": gateway_id,
        "X-BoxTeam-Workspace-Id": workspace_id,
    })

    session_data = _api_data(await client.post("/api/v1/sessions", json={"title": "Thread 附件来源"}))
    session_id = cast(str, session_data["session_id"])
    main_thread = _api_data(await client.get(f"/api/v1/sessions/{session_id}/main-thread"))
    thread_id = cast(str, main_thread["thread_id"])
    config = commentjson.loads((workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
    primary = next(provider for provider in config["llm"]["providers"] if provider["id"] == "primary")
    model_calls = replay.control.plan_model_calls(
        session_id,
        thread_id,
        _completion(primary["model"], primary["endpoint"].rstrip("/") + "/chat/completions"),
    )

    text_name = "thread-quick-access-preview.txt"
    media_name = "thread-quick-access-media.pdf"
    text_content = "Thread Quick Access 原件预览 Ω\n"
    media_content = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n%%EOF\n"
    attachments = [_inline_attachment(text_name, "text/plain", text_content.encode("utf-8"))]
    attachments.extend(
        _inline_attachment(f"thread-quick-access-{index:03d}.bin", "application/octet-stream", b"small attachment")
        for index in range(99)
    )
    attachments.append(_inline_attachment(media_name, "application/pdf", media_content))
    sent = await client.post(
        f"/api/v1/sessions/{session_id}/threads/{thread_id}/messages",
        json={
            "message": {"content": "请确认已收到这些附件。", "attachments": attachments},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    accepted = _api_data(sent)
    assert accepted["thread_id"] == thread_id
    await wait_for_job_done(client, cast(str, accepted["job_id"]))
    assert not (workspace / text_name).exists()
    assert not (workspace / media_name).exists()
    attachment_page_response = await client.get(
        f"/api/v1/sessions/{session_id}/threads/{thread_id}/attachments",
        params={"limit": "100"},
    )
    first_attachment_page = _api_data(attachment_page_response)
    assert first_attachment_page["session_id"] == session_id
    assert first_attachment_page["thread_id"] == thread_id
    assert len(first_attachment_page["items"]) == 100 and first_attachment_page["next_after"]
    (context.artifacts_dir / "attachment-metadata-preflight.json").write_text(
        json.dumps(first_attachment_page, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    result_path = context.artifacts_dir / "thread-attachments-browser.json"
    screenshot_path = context.artifacts_dir / "thread-attachments-browser.png"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [
        sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "240", "--",
        "node", "tests/integration/clients/web/quick_access/resources/attachments.mjs",
    ]
    browser_fixture = {
        "gatewayId": gateway_id,
        "workspaceId": workspace_id,
        "sessionId": session_id,
        "threadId": thread_id,
        "textName": text_name,
        "textContent": text_content,
        "mediaName": media_name,
    }
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=context.project_root,
            env={
                **os.environ,
                **temporary,
                "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                "BOXTEAM_BROWSER_FIXTURE": json.dumps(browser_fixture, ensure_ascii=False),
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
            },
            capture_output=True,
            text=True,
            timeout=255,
            check=False,
        )
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(
        json.dumps({"command": command, "exit_code": completed.returncode}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert all(result[key] for key in (
        "browseOnly", "filterAndCancel", "pagination", "textPreview", "mediaPreview", "sideFocus", "stateRestored",
    ))
    assert result["browseContentReads"] == []
    assert result["workspaceContentReads"] == []
    pages = result["attachmentPages"]
    media_pages = [item for item in pages if item["phase"] == "media-preview"]
    assert media_pages
    first_media_page = media_pages[0]["data"]
    media_in_first_page = any(item["file_id"] == result["mediaFileId"] for item in first_media_page["items"])
    assert len(media_pages) == (1 if media_in_first_page else 2), media_pages
    phases = Counter(item["phase"] for item in pages)
    assert phases["browse"] == 1 and phases["pagination"] == 2
    assert phases["media-preview"] == len(media_pages) and len(pages) == 3 + len(media_pages), pages
    assert all(item["status"] == 200 and item["request_id"] == item["header"] for item in pages), pages
    assert all(item["path"] == f"/api/v1/sessions/{session_id}/threads/{thread_id}/attachments" for item in pages)
    assert all(item["data"]["session_id"] == session_id and item["data"]["thread_id"] == thread_id for item in pages), pages
    assert all(item["gatewayId"] == gateway_id and item["workspaceId"] == workspace_id for item in pages), pages
    first = next(item["data"] for item in pages if item["phase"] == "browse")
    page_one = next(item["data"] for item in pages if item["phase"] == "pagination" and not item["after"])
    next_page_record = next(item for item in pages if item["phase"] == "pagination" and item["after"])
    next_page = next_page_record["data"]
    assert len(first["items"]) == 100 and first["next_after"], first
    assert len(page_one["items"]) == 100 and page_one["next_after"], page_one
    assert len(next_page["items"]) == 1 and next_page["next_after"] is None, next_page
    assert next_page_record["after"] == page_one["next_after"]
    if len(media_pages) == 2:
        assert first_media_page["next_after"] and media_pages[1]["after"] == first_media_page["next_after"]
    assert any(item["file_id"] == result["mediaFileId"] for page in media_pages for item in page["data"]["items"])
    for page in (first, page_one, next_page):
        for item in page["items"]:
            assert not {"data_url", "content", "body", "bytes"}.intersection(item), item
    assert Counter((item["fileId"], item["phase"]) for item in result["contentReads"]) == Counter({
        (result["textFileId"], "text-preview"): 1,
        (result["mediaFileId"], "media-preview"): 1,
        (result["textFileId"], "restored"): 1,
        (result["mediaFileId"], "restored"): 1,
    })
    with replay.control._condition:
        calls = {key: count for key, count in replay.control._counters.items() if key.startswith("model_call/")}
    assert len(model_calls) == 1 and sum(calls.values()) == 1, calls
