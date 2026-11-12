"""Session、Thread 候选和原会话导航的分支动作经过真实 Gateway 与 Workbench。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
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


def _source_interaction(workspace: Path) -> dict[str, object]:
    config = commentjson.loads((workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
    primary = next(item for item in config["llm"]["providers"] if item["id"] == "primary")
    cassette = build_cassette(
        asset_id="session-fork-actions",
        model=primary["model"],
        url=f"{primary['endpoint'].rstrip('/')}/chat/completions",
        frames=chat_completion_frames(
            response_id="session-fork-actions",
            model=primary["model"],
            deltas=[{"role": "assistant", "content": "用于分支验收的来源消息。"}],
            finish_reason="stop",
        ),
    )
    interactions = cassette.raw["interactions"]
    assert len(interactions) == 1
    return interactions[0]


async def _configure_gateway_url(
    client: httpx.AsyncClient, *, gateway_id: str, workspace_id: str,
) -> None:
    source_path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    loaded = await client.get(source_path, headers=headers)
    loaded.raise_for_status()
    document = loaded.json()["data"]
    config = commentjson.loads(document["jsonc"])
    config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})[
        "url"
    ] = str(client.base_url).rstrip("/")
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


@pytest.fixture(autouse=True)
def session_fork_web_assets(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    prepared = os.getenv("BOXTEAM_SOURCE_WEB_ASSETS")
    assets = Path(prepared) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))
    return assets


async def test_session_and_thread_fork_actions_keep_the_exact_source(
    request: pytest.FixtureRequest,
    history_target: HistoryTarget,
    prepared_developer_tools_package: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", LOCAL_TOKEN_HEADERS["X-Local-Token"])
    history_target.restart()
    workspace = history_target.workspace
    replay = history_target.replay
    web_assets = request.getfixturevalue("session_fork_web_assets")
    browser_binary = (os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
                      or shutil.which("chromium") or shutil.which("chromium-browser"))
    assert browser_binary, "Session 分支页面验收需要 Chromium"

    result_path = context.artifacts_dir / "session-fork-actions-browser.json"
    screenshot_path = context.artifacts_dir / "session-fork-actions-failure.png"
    supervisor = context.project_root / "app/runtime" / (
        "windows_job.py" if sys.platform == "win32" else "posix_job.py"
    )
    command = ["node", "tests/integration/clients/web/quick_access/resources/session_fork_actions.mjs"]
    supervised_command = [
        sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
        "--timeout-seconds", "210", "--", *command,
    ]
    copy_prepared_developer_tools_package(
        prepared_developer_tools_package, workspace.parent / "boxteam-home",
    )
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{history_target.gateway_port}",
        headers=LOCAL_TOKEN_HEADERS,
        timeout=90,
    ) as client:
        health_response = await client.get("/api/gateway/health")
        assert health_response.status_code == 200, health_response.text
        health_payload = health_response.json()
        assert health_payload["request_id"] == health_response.headers["X-Request-ID"]
        routes_response = await client.get("/api/gateway/workspaces?check_health=false")
        assert routes_response.status_code == 200, routes_response.text
        owner = routes_response.json()["data"]["active_workspace"]
        assert owner["gateway_id"] == health_payload["data"]["gateway_id"]
        await _configure_gateway_url(
            client, gateway_id=owner["gateway_id"], workspace_id=owner["workspace_id"],
        )
        await enable_developer_tools_plugins(
            client, gateway_id=owner["gateway_id"], workspace_id=owner["workspace_id"],
            plugin_ids=("terminal", "browser"),
        )
        with chromium_temporary_environment(
            context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp",
        ) as temporary:
            completed = await asyncio.to_thread(
                subprocess.run,
                supervised_command,
                cwd=context.project_root,
                env={
                    **os.environ,
                    **temporary,
                    **replay.environment,
                    "BOXTEAM_SOURCE_WEB_ASSETS": str(web_assets),
                    "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                    "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                    "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                    "BOXTEAM_THREAD_FORK_INTERACTIONS": json.dumps(
                        [_source_interaction(workspace)], ensure_ascii=False,
                    ),
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": browser_binary,
                },
                capture_output=True,
                text=True,
                timeout=225,
                check=False,
            )
    (context.artifacts_dir / "session-fork-actions-browser.stdout.raw").write_text(
        completed.stdout, encoding="utf-8",
    )
    (context.artifacts_dir / "session-fork-actions-browser.stderr.raw").write_text(
        completed.stderr, encoding="utf-8",
    )
    (context.artifacts_dir / "session-fork-actions-browser-process.json").write_text(
        json.dumps({
            "command": command,
            "supervised_command": supervised_command,
            "exit_code": completed.returncode,
            "workbench_assets": str(web_assets),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete", result
    assert result["pageErrors"] == []
    assert result["threadCandidateCancel"] is True
    assert result["sessionCandidateCancel"] is True
    assert result["navigationCancel"] is True
    assert result["fullRolloutCopy"] is True
    assert result["childThreadF1UsesExactSource"] is True
    assert len(result["forkWrites"]) == 1
    assert result["forkWrites"][0]["body"]["mode"] == "full_rollout_copy"
    assert result["forkWrites"][0]["body"]["anchor_item_id"] is None
    assert result["forkWrites"][0]["body"]["creation_kind"] == "new_child_thread"
    assert result["forkWrites"][0]["body"]["source_uri"].endswith(
        f"/threads/{result['mainThreadId']}"
    )
    assert len(result["messageWrites"]) == 1
    assert result["messageWrites"][0]["phase"] == "create-history"
    assert result["sourcePointReceipts"]
    assert all(item["receipt"]["status"] == 200
               and item["receipt"]["requestId"] == item["receipt"]["header"]
               for item in result["sourcePointReceipts"])
    with replay.control._condition:
        model_calls = sum(count for key, count in replay.control._counters.items()
                          if key.startswith("model_call/"))
    assert model_calls == 1, "只有准备 fork 来源消息时可以调用一次模型"
