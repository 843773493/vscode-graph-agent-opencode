"""真实存储权限失败的 Web 提示、重试与工具历史恢复；模型使用精确回放。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 — fixture 依赖。
)
from tests.integration.clients.web.itemized_context.test_fork_prefix_recovery import (
    prefix_runtime as prefix_runtime,  # noqa: PLC0414 — 按当前测试路径分配独立服务和工作区。
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS
from tests.support.processes.scope import capture_test_processes, register_test_process
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.skipif(os.name != "posix", reason="验证 POSIX 文件权限；不代替 Windows 原生验收")
async def test_real_lock_permission_failure_is_visible_and_retry_preserves_history(
    request: pytest.FixtureRequest,
    prefix_runtime: tuple[str, Path, ModelStreamReplay, int],
) -> None:
    base_url, workspace, replay, _ = prefix_runtime
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "历史故障 Web 验收需要真实 Chromium"
    async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60) as client:
        guest = await client.post("/api/gateway/users/guest", json={"tracking": {"source": "history-storage-failure"}})
        assert guest.status_code == 200, guest.text
        registry = await client.get("/api/gateway/workspaces")
        assert registry.status_code == 200, registry.text
        route_id = registry.json()["data"]["active_workspace_id"]
        client.headers["X-BoxTeam-Workspace-Id"] = route_id
        created = await client.post("/api/v1/sessions", json={"title": "真实存储故障恢复", "agent_id": "default"})
        assert created.status_code == 200, created.text
        session = created.json()["data"]
        session_id = session["session_id"]
        updated = await client.patch(f"/api/v1/sessions/{session_id}", json={"provider_id": "primary"})
        assert updated.status_code == 200 and updated.json()["data"]["current_provider_id"] == "primary", updated.text
        main = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
        assert main.status_code == 200, main.text
        thread_id = main.json()["data"]["thread_id"]
    resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
    thread_root = resolver.resolve_thread_node(session_id, thread_id)
    ports = integration_port_block_for_file(Path(request.node.path))
    responses = [
        ([{"role": "assistant", "tool_calls": [{"index": 0, "id": "history-permission-read", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'}}]}], "tool_calls"),
        ([{"role": "assistant", "content": "存储故障前的完整工具历史。"}], "stop"),
        ([{"role": "assistant", "content": "权限恢复后原 Thread 继续成功。"}], "stop"),
    ]
    cassettes = [build_cassette(
        asset_id="web-history-storage-failure", model="browser-chat-tool-loop-stub",
        url=f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
        frames=chat_completion_frames(response_id=f"history-permission-{index}", model="browser-chat-tool-loop-stub",
            deltas=deltas, finish_reason=finish),
    ).raw for index, (deltas, finish) in enumerate(responses)]
    environment = os.environ | replay.environment | {
        "BOXTEAM_BROWSER_BASE_URL": base_url,
        "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(artifacts),
        "BOXTEAM_BROWSER_TEST_OWNER": str(request.node.path),
        "BOXTEAM_BROWSER_OWNER": json.dumps({"routeId": route_id, "workspaceId": session["workspace_id"],
            "sessionId": session_id, "threadId": thread_id}),
        "BOXTEAM_BROWSER_LOCK_PATH": str(thread_root / ".rollout.write.lock"),
        "BOXTEAM_BROWSER_CANONICAL_PATH": str(thread_root / "rollout/rollout.jsonl"),
        "BOXTEAM_BROWSER_MODEL_CASSETTES": json.dumps(cassettes),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    command = ["node", "tests/integration/clients/web/itemized_context/history_storage_failure.mjs"]
    with (
        chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary,
        (artifacts / "browser.log").open("x", encoding="utf-8") as output,
        capture_test_processes(),
    ):
        process = await asyncio.to_thread(subprocess.Popen, command, env=environment | temporary, stdout=output, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
        managed = ManagedProcess(process=process, log_file=output)
        register_test_process(managed, managed.close)
        returncode = await asyncio.to_thread(process.wait, timeout=180)
    (artifacts / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": returncode}, indent=2))
    assert returncode == 0, (artifacts / "browser.log").read_text()
    facts = json.loads((artifacts / "history-storage-result.json").read_text())
    assert facts["phase"] == "complete" and facts["pageErrors"] == []
    assert facts["owner"] == json.loads(environment["BOXTEAM_BROWSER_OWNER"])
    assert facts["initialHistory"] == facts["recoveredHistory"]
    assert facts["failure"]["status"] == 500
    assert facts["failure"]["body"]["detail"]["code"] == "turn_projection_corrupted"
    assert facts["nativeFailure"]["code"] == "EACCES"
    assert facts["failure"]["body"]["detail"]["message"] in facts["failure"]["visibleMessage"]
    continued = facts["continuedHistory"]
    assert continued["source_view_revision"] > facts["initialHistory"]["source_view_revision"]
    assert continued["items"][0] == {**facts["initialHistory"]["items"][0],
        "source_view_id": continued["source_view_id"], "source_view_revision": continued["source_view_revision"]}
    calls = facts["modelCalls"]
    assert len(calls) == len(set(calls)) == 3
    captures = {call: json.loads((replay.artifacts / f"{call}.json").read_text()) for call in calls}
    assert all(record["identity"] == {"session_id": session_id, "thread_id": thread_id, "model_call_id": call}
               for call, record in captures.items())
    initial, final, successor = [captures[call]["request"]["messages"] for call in calls]
    assert final[:-2] == initial
    assistant, result = final[-2:]
    assert [assistant["role"], result["role"]] == ["assistant", "tool"]
    assert assistant["tool_calls"][0]["id"] == result["tool_call_id"] == "history-permission-read"
    assert "# 统一测试工作区" in result["content"]
    assert successor[:len(final)] == final
    assert successor[len(final):] == [
        {"role": "assistant", "content": [{"text": "存储故障前的完整工具历史。", "type": "text"}]},
        {"role": "user", "content": "恢复后继续原 Thread，保留之前的工具结果。"},
    ]
    audit = audit_sealed_requests(thread_root / "rollout/index.sqlite",
        (resolver.resolve_session_node(session_id) / "logs/llm_requests").glob("*.json"), captures)
    assert isinstance(audit["requests"], list) and len(audit["requests"]) == 3
    (artifacts / "sealed-request-audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2))
