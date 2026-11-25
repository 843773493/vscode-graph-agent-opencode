"""Thread 的规范资源引用经过真实 Gateway、产品 Thread 编辑器与剪贴板。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

import commentjson
import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    HistoryTarget,
)
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    history_target as history_target,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)


@pytest.fixture(autouse=True)
def thread_reference_web_assets(resource_web_assets: Path) -> Path:
    return resource_web_assets


def _interaction(workspace: Path) -> dict[str, object]:
    config = commentjson.loads((workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
    primary = config["settings"]["workspace"]["llm"]["providers"]["primary"]
    model = primary["models"]["default"]["model"]
    endpoint = config["profiles"]["provider.primary"]["settings"]["model_options"]["endpoint"]
    cassette = build_cassette(
        asset_id="thread-resource-reference",
        model=model,
        url=f"{endpoint.rstrip('/')}/chat/completions",
        frames=chat_completion_frames(
            response_id="thread-resource-reference",
            model=model,
            deltas=[{"role": "assistant", "content": "已创建用于 Thread 引用的产品历史。"}],
            finish_reason="stop",
        ),
    )
    interactions = cast(list[dict[str, object]], cassette.raw["interactions"])
    assert len(interactions) == 1
    return interactions[0]


def _restart_backend_with_runtime_control(
    target: HistoryTarget, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", LOCAL_TOKEN_HEADERS["X-Local-Token"])
    target.restart()


async def _configure_gateway_url(
    client: httpx.AsyncClient, *, gateway_id: str, workspace_id: str, gateway_url: str,
) -> None:
    source_path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    loaded = await client.get(source_path, headers=headers)
    loaded.raise_for_status()
    document = loaded.json()["data"]
    config = commentjson.loads(document["jsonc"])
    config["settings"]["workspace"].setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = gateway_url
    saved = await client.put(source_path, headers=headers, json={
        "gateway_id": gateway_id,
        "workspace_id": workspace_id,
        "source_key": "workspace_root_mutable_override",
        "jsonc": json.dumps(config, ensure_ascii=False, indent=2),
        "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"],
        "idempotency_key": uuid4().hex,
    })
    saved.raise_for_status()
    assert saved.json()["data"]["edit"]["state"] == "active", saved.text


@pytest.mark.asyncio
async def test_thread_main_child_reference_copy_and_composer_file_picker(
    request: pytest.FixtureRequest,
    history_target: HistoryTarget,
    prepared_developer_tools_package: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    _restart_backend_with_runtime_control(history_target, monkeypatch)
    workspace = history_target.workspace
    replay = history_target.replay
    web_workbench_assets = request.getfixturevalue("thread_reference_web_assets")
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    assert chromium, "Thread 规范引用页面验收需要 Chromium"
    (workspace / "thread-reference-source.txt").write_text("未打开的文件正文不能读取", encoding="utf-8")
    interaction = _interaction(workspace)
    copy_prepared_developer_tools_package(
        prepared_developer_tools_package, workspace.parent / "boxteam-home",
    )
    result_path = context.artifacts_dir / "thread-references-browser.json"
    screenshot_path = context.artifacts_dir / "thread-references-failure.png"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = ["node", "tests/integration/clients/web/quick_access/resources/thread_references.mjs"]
    supervised_command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                          "--timeout-seconds", "210", "--", *command]
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{history_target.gateway_port}",
        headers=LOCAL_TOKEN_HEADERS,
        timeout=90,
    ) as client:
        health_response = await client.get("/api/gateway/health")
        assert health_response.status_code == 200, health_response.text
        health_payload = health_response.json()
        assert health_payload["request_id"] == health_response.headers["X-Request-ID"]
        health = health_payload["data"]
        workspace_response = await client.get("/api/gateway/workspaces?check_health=false")
        assert workspace_response.status_code == 200, workspace_response.text
        workspaces = workspace_response.json()["data"]
        owner = workspaces["active_workspace"]
        assert owner["gateway_id"] == health["gateway_id"]
        await _configure_gateway_url(
            client, gateway_id=owner["gateway_id"], workspace_id=owner["workspace_id"],
            gateway_url=str(client.base_url).rstrip("/"),
        )
        await enable_developer_tools_plugins(
            client, gateway_id=owner["gateway_id"], workspace_id=owner["workspace_id"],
            plugin_ids=("terminal", "browser"),
        )
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(
                subprocess.run,
                supervised_command,
                cwd=context.project_root,
                env={**os.environ, **temporary, **replay.environment,
                     "BOXTEAM_SOURCE_WEB_ASSETS": str(web_workbench_assets),
                     "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                     "BOXTEAM_THREAD_REFERENCE_INTERACTIONS": json.dumps([interaction], ensure_ascii=False),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True,
                text=True,
                timeout=225,
                check=False,
            )
    (context.artifacts_dir / "thread-references-browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "thread-references-browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "thread-references-browser-process.json").write_text(json.dumps({
        "command": command,
        "supervised_command": supervised_command,
        "exit_code": completed.returncode,
        "workbench_assets": str(web_workbench_assets),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete", result
    assert result["pageErrors"] == []
    assert result["mainAndChildReferences"] is True
    assert result["threadComposerKeptFilePicker"] is True
    assert len(result["threadAddresses"]) == 2
    assert all(item["receipt"]["status"] == 200
               and item["receipt"]["requestId"] == item["receipt"]["header"]
               for item in result["threadAddresses"])
    assert result["resourceReads"] == []
    assert len(result["executions"]) == 1
    assert result["executions"][0]["path"] == f"/api/v1/sessions/{result['sessionId']}/threads/{result['mainThreadId']}/messages"
    file_selection_addresses = [item for item in result["fileAddresses"]
                                if item["phase"] == "thread-composer-file-selection"]
    assert len(file_selection_addresses) == 1
    assert "thread_id" not in file_selection_addresses[0]["body"]
