"""产品打开与关闭生成最近历史，正常刷新后由原历史恢复精确归属。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
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
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_data,
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(autouse=True)
def metadata_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_HISTORY_WEB_ASSETS")
    assets = Path(prepared) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


@pytest.fixture
async def history_cache_case(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, ModelStreamReplay],
) -> AsyncIterator[tuple[httpx.AsyncClient, TestRunContext, ModelStreamReplay]]:
    backend_url, workspace, replay = browser_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=backend_url,
        port=integration_port_block_for_file(Path(request.node.path)).port(20),
        artifacts_dir=context.artifacts_dir / "cache-gateway",
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}",
                                    headers={"X-Local-Token": "local-dev-token"}, timeout=60) as client:
            yield client, context, replay
    finally:
        close_gateway_process(gateway)


async def test_closed_resources_survive_history_reload_without_deleting_business_state(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "最近历史验收需要 Chromium"
    (workspace / "history.txt").write_text("A history file", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("A history source", encoding="utf-8")
    other = prepare_default_test_workspace(
        workspace_root=context.workspace_root / "history-B",
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True,
    )
    install_test_workspace_config(workspace_root=other, config_path=workspace / ".boxteam/workspace.jsonc",
                                  schema_path=context.project_root / "configs/workspace_schema.jsonc")
    output = context.artifacts_dir / "recent-history.json"
    command = ["node", "tests/integration/clients/web/quick_access/recent_history.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_HISTORY_OTHER_WORKSPACE": str(other),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=150, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(output.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert result["restoredClosedHistory"] and result["reopenDeduplicated"] and result["removedHistoryOnly"]
    assert result["draftRestored"] and result["businessPreserved"] and result["scopedOwners"]
    assert [item["mode"] for item in result["recovery"]] == ["unknown-version", "missing-owner", "broken-json"]
    assert all(item["retainedAcrossReload"] and item["displayOnlyRemoval"] and item["targetReads"] == 0
               for item in result["recovery"])
    assert result["searches"] == [] and result["executions"] == []
    assert (workspace / "history.txt").read_text(encoding="utf-8") == "A history file"
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "浏览历史和编辑草稿不得触发模型调用"


@dataclass(frozen=True)
class ThreadCacheCleanupCase:
    context: TestRunContext
    gateway_url: str
    provider: ModelStreamReplay
    recovery_file: Path
    root_uri: str
    threads: list[dict[str, str]]


@pytest.fixture
async def cache_cleanup_case(
    request: pytest.FixtureRequest,
    history_cache_case: tuple[httpx.AsyncClient, TestRunContext, ModelStreamReplay],
    browser_backend: tuple[str, Path, ModelStreamReplay],
) -> ThreadCacheCleanupCase:
    client, context, provider = history_cache_case
    health = await metadata_data(client, "/api/gateway/health")
    targets = await metadata_data(client, "/api/gateway/workspaces")
    owner = targets["active_workspace"]
    assert owner["gateway_id"] == health["gateway_id"]
    client.headers.update({"X-BoxTeam-Target-Gateway-Id": owner["gateway_id"],
                           "X-BoxTeam-Workspace-Id": owner["workspace_id"]})
    roots = await metadata_data(client, "/api/v1/workspace/files/roots")
    recovery_file = browser_backend[1] / "cache-protected.txt"
    recovery_file.write_text("原始磁盘正文\n", encoding="utf-8")
    endpoint = integration_port_block_for_file(Path(request.node.path)).port(10)
    identities: list[dict[str, str]] = []
    for label in ("A", "B"):
        response = await client.post("/api/v1/sessions", json={"title": f"缓存清理 {label}"})
        assert response.status_code == 200, response.text
        created = response.json()["data"]
        session_id, thread_id = created["session_id"], created["thread_id"]
        answer = f"缓存助手 {label} 的真实历史正文"
        cassette = build_cassette(asset_id=f"history-cache-{label.lower()}", model="e2e-stub-model",
            url=f"http://127.0.0.1:{endpoint}/v1/chat/completions",
            frames=chat_completion_frames(response_id=f"history-cache-{label}", model="e2e-stub-model",
                deltas=[{"role": "assistant", "content": answer}], finish_reason="stop"))
        provider.control.plan_model_calls(session_id, thread_id,
            cast(list[dict[str, object]], cassette.raw["interactions"]))
        accepted = await client.post(f"/api/v1/sessions/{session_id}/threads/{thread_id}/messages",
                                     json={"message": {"content": f"缓存用户 {label}"}, "run": {}})
        assert accepted.status_code == 200, accepted.text
        job_id = accepted.json()["data"]["job_id"]
        async with asyncio.timeout(45):
            while True:
                job = await metadata_data(client, f"/api/v1/jobs/{job_id}")
                assert job["status"] not in {"failed", "cancelled", "timed_out", "execution_lost"}, job
                if job["status"] == "completed":
                    break
                await asyncio.sleep(0.1)
        identities.append({**owner, "session_id": session_id, "thread_id": thread_id, "answer": answer})
    return ThreadCacheCleanupCase(context, str(client.base_url).rstrip("/"), provider,
                                  recovery_file, roots["workspace_uri"], identities)


async def test_clear_thread_history_cache_preserves_business_and_recovery(
    request: pytest.FixtureRequest,
    cache_cleanup_case: ThreadCacheCleanupCase,
) -> None:
    case = cache_cleanup_case
    context = case.context
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "历史缓存清理验收需要 Chromium"
    output = context.artifacts_dir / "thread-cache-cleanup.json"
    command = ["node", "tests/integration/clients/web/quick_access/thread_cache_cleanup.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": case.gateway_url,
                 "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_CACHE_THREADS": json.dumps(case.threads),
                 "BOXTEAM_CACHE_FILE_ROOT_URI": case.root_uri,
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=150, check=False)
    (context.artifacts_dir / "cache-browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "cache-browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "cache-browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert_cache_cleanup_result(output, case)


def assert_cache_cleanup_result(output: Path, case: ThreadCacheCleanupCase) -> dict:
    result = json.loads(output.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert all(result[key] for key in ("commandDiscoverable", "cancelPreserved", "capturedOwnerCleared",
                                      "otherThreadPreserved", "draftsRestored", "settingsRestored", "historyRebuilt"))
    assert result["forbiddenWrites"] == []
    assert all(result[key] for key in ("automaticLocalHistory", "manualLocalHistory", "timelineOwnerPreserved",
                                      "localHistoryPreserved", "dirtyBackupRestored", "splitViewsRestored"))
    assert case.recovery_file.read_text(encoding="utf-8").startswith("已保存的历史正文\n")
    assert "尚未保存的正文" not in case.recovery_file.read_text(encoding="utf-8")
    with case.provider.control._condition:
        calls = [count for key, count in case.provider.control._counters.items() if key.startswith("model_call/")]
    assert sorted(calls) == [1, 1], "清理、打开和恢复不得新增模型调用"
    return result
