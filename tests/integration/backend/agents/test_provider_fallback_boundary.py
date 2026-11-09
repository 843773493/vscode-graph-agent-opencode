"""真实 HTTP/SDK 工具参数流失败，必须保留故障事实并拒绝未闭合的备用派发。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path
from typing import Literal

import commentjson
import httpx
import pytest

from app import main
from app.core.path_utils import get_session_path_resolver
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.serialization import normalize_wire_request
from app.protocol.codecs.message_stream import (
    message_stream_to_json,
    message_stream_to_proto,
)
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    NativeHTTPState,
)
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    native_http_server as native_http_server,  # noqa: PLC0414 - 复用 HTTP 服务及唯一 SSE 编码
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    _prepare_source_workspace,
)
from tests.support.api_helpers import create_session


@pytest.fixture
async def fallback_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
    native_http_server: tuple[NativeHTTPState, str],
) -> AsyncIterator[tuple[httpx.AsyncClient, Path, TestRunContext, NativeHTTPState]]:
    """独立工作区装配真实后端；本测试专门保留 TCP 与 SDK 的失败边界。"""
    state, endpoint = native_http_server
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = httpx.URL(endpoint).port
    assert port is not None
    workspace = _prepare_source_workspace(context, port)
    config_path = workspace / ".boxteam/workspace.jsonc"
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    for provider in config["llm"]["providers"]:
        # 真 HTTP 用 SDK 已登记的原生流模型，避免虚构名称触发 LiteLLM fake_stream。
        provider.update(endpoint=endpoint, api_key=state.api_key, model="openai/gpt-4.1")
        provider["api_mode"]["protocol"] = "responses" if provider["id"] == "primary" else "chat_completions"
        if provider["id"] == "primary":
            provider["api_mode"]["supports_reasoning"] = {"reasoning_items": {"summary": True, "encrypted_content": False}}
    config["agents"]["default"]["model"]["fallback_providers"] = ["backup_1"]
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    workspace_id = load_or_create_workspace_id(workspace)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home_for_node(request.node.nodeid)))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    monkeypatch.delenv("BOXTEAM_GATEWAY_URL", raising=False)
    monkeypatch.delenv("BOXTEAM_TEST_MODEL_STREAM_CONFIG", raising=False)
    monkeypatch.setattr(main, "_model_stream_controller", None)
    monkeypatch.setenv("BOXTEAM_MOUNTED_WORKSPACES", json.dumps([{
        "workspace_id": workspace_id, "workspace_root": str(workspace),
        "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None,
    }]))
    async with main.lifespan(main.app) as _main_lifespan_state_76, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": workspace_id},
        timeout=30,
    ) as client:
        await _main_lifespan_state_76["after_http_startup"]()
        yield client, workspace, context, state


@pytest.mark.parametrize("failure", ["http", "stream", "partial_tool"])
async def test_product_fallback_keeps_failed_attempt_and_rejects_unclosed_tool_group(
    fallback_backend: tuple[httpx.AsyncClient, Path, TestRunContext, NativeHTTPState],
    caplog: pytest.LogCaptureFixture, failure: Literal["http", "stream", "partial_tool"],
) -> None:
    client, workspace, context, state = fallback_backend
    state.responses_failure = failure
    if failure == "partial_tool":
        state.output_tool_name = "read_file"
    session = await create_session(client, "工具参数流失败的备用派发边界")
    with caplog.at_level(logging.ERROR):
        sent = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "保留候选失败事实；不要执行未完成的工具调用。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert sent.status_code == 200, sent.text
        assert sent.headers["X-Request-ID"] == sent.json()["request_id"]
        accepted = sent.json()["data"]
        async with asyncio.timeout(45):
            while True:
                result = await client.get(f"/api/v1/jobs/{accepted['job_id']}")
                assert result.status_code == 200, result.text
                job = result.json()["data"]
                if job["status"] in {"completed", "failed", "cancelled", "timed_out", "execution_lost"}:
                    break
                await asyncio.sleep(0.05)
    assert job["status"] == ("failed" if failure == "partial_tool" else "completed"), job
    thread = str(accepted["thread_id"])
    response = await client.get(
        f"/api/v1/sessions/{session}/turns/{accepted['turn_id']}/message-stream/events",
        params={"thread_id": thread},
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    events = response.json()["data"]
    failed, = [event for event in events if event["type"] == "model.failed"]
    primary_call = failed["payload"]["model_call_id"]
    assert failed["payload"]["outcome"] == "upstream_error"
    assert "provider_id=primary" in failed["payload"]["message"]
    assert failed["payload"]["error_code"] != "ToolProtocolBoundaryConflict"
    assert all(event["session_id"] == session and event["thread_id"] == thread for event in events)
    assert not any(event["type"] in {"tool.started", "tool.completed"} for event in events)
    resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
    rollout = resolver.resolve_thread_node(session, thread) / "rollout"
    original = (rollout / "rollout.jsonl").read_bytes()
    items = [json.loads(line) for line in original.splitlines()]
    logs = [json.loads(path.read_text(encoding="utf-8"))
            for path in (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json")]
    log, = [log for log in logs if log["job_id"] == accepted["job_id"]]
    assert log["upstream"]["attempts"][0]["error"]
    assert not any(item["semantic_kind"] == "tool_result" for item in items)
    with RolloutCheckpointSaver(workspace / ".boxteam/sessions") as cold:
        assemblies = cold.list_context_assemblies(session, thread)
        assert len(assemblies) == (1 if failure == "partial_tool" else 2)
        assert assemblies[0].model_call_id == primary_call
        for assembly, request in zip(assemblies, state.requests, strict=True):
            assert assembly.request_hash_preimage == normalize_wire_request(request["body"])
            assert assembly.request_hash == context_request_hash(
                assembly.as_sealed_plan(), assembly.provider_version,
                projector_id=assembly.projector_id, projector_version=assembly.projector_version,
                target_format=assembly.target_format, wire_request=assembly.request_hash_preimage,
            )
    with closing(sqlite3.connect((rollout / "index.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        calls = [dict(row) for row in connection.execute("SELECT * FROM model_calls ORDER BY attempt")]
    assert calls[0]["outcome"] == "failed" and calls[0]["model_call_id"] == primary_call
    assert all((call["session_id"], call["thread_id"], call["execution_id"]) ==
               (session, thread, assemblies[0].execution_id) for call in calls)
    if failure == "partial_tool":
        assert [request["path"] for request in state.requests] == ["/v1/responses"]
        assert len(log["upstream"]["attempts"]) == 1
        assert events[-1]["type"] == "stream.failed"
        assert events[-1]["payload"]["message"] == job["error_message"]
        assert "tool-protocol-boundary-conflict" in job["error_message"]
        call, = [item for item in items if item["semantic_kind"] == "tool_call"]
        assert call["status"] == "failed" and call["payload"]["name"] == "read_file"
        assert call["projection_identity"]["model_call_id"] == primary_call
        unfinished, = [event for event in events if event["type"] == "tool_call.completed"]
        assert unfinished["payload"]["arguments_complete"] is False
        assert unfinished["payload"]["status"] == "incomplete"
        assert events.index(failed) < events.index(unfinished)
        assert len(calls) == 1
        # TODO：若需自动越过失败工具分片，先定义显式上下文选集转换；不得隐藏历史或合成结果来解锁 fallback。
    else:
        assert [request["path"] for request in state.requests] == ["/v1/responses", "/v1/chat/completions"]
        assert len(log["upstream"]["attempts"]) == 2
        assert log["upstream"]["attempts"][-1]["error"] is None
        assert events[-1]["type"] == "stream.completed"
        assert calls[-1]["outcome"] == "completed" and len(calls) == 2
        assert not any(item["semantic_kind"] == "tool_call" for item in items)
    assert any(record.exc_info and type(record.exc_info[1]).__name__ == failed["payload"]["error_code"]
               for record in caplog.records)
    live_store = main.app.state.workspace_container_registry.get(load_or_create_workspace_id(workspace)).message_stream_store
    stored = await live_store.list_events(session_id=session, turn_stream_id=failed["turn_stream_id"])
    assert [message_stream_to_json(message_stream_to_proto(event)) for event in stored] == events
    cold_stream = MessageStreamStore(path_resolver=resolver)
    await cold_stream.open_existing(
        session_id=session, thread_id=thread, turn_id=str(accepted["turn_id"]),
        turn_stream_id=failed["turn_stream_id"],
    )
    assert await cold_stream.list_events(session_id=session, turn_stream_id=failed["turn_stream_id"]) == stored
    assert (rollout / "rollout.jsonl").read_bytes() == original
    assert str(workspace) not in json.dumps([job, events], ensure_ascii=False)
    (context.artifacts_dir / f"fallback-{failure}-actual.json").write_text(json.dumps({
        "job": job, "events": events, "canonical_items": items,
        "model_calls": calls,
        "assemblies": [assembly.to_dict() for assembly in assemblies],
        "requests": state.requests,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
