"""真实 Gateway／Session／Agent／stdio 结果链，模型响应复用 model_stream。"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import commentjson
import httpx
import pytest

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.serialization import normalize_wire_request
from app.schemas.internal_v2.mcp import McpInvocationReceipt
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    acheckpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents import test_debug_prompt_flow as replay_case
from tests.integration.workspace_services.mcp.schema_server import (
    RESULT_AUDIO_BASE64,
    RESULT_IMAGE_BASE64,
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import workspace_backend_id_for_root

scripted_model_replay = replay_case.scripted_model_replay
scripted_debug_backend_port = replay_case.scripted_debug_backend_port
scripted_backend_process = replay_case.scripted_backend_process


@pytest.fixture(scope="module")
def scripted_model_info() -> dict[str, bool]:
    return {"supports_function_calling": True, "supports_reasoning": True,
            "supports_audio_input": True, "supports_vision": True}


@pytest.fixture(scope="module")
def result_server_receipts_path(request: pytest.FixtureRequest) -> Path:
    # artifacts_dir 按当前测试节点动态计算；冻结 Server 启动时真正使用的收据路径。
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    return context.artifacts_dir / "receipts.jsonl"


@pytest.fixture(scope="module")
def integration_workspace_config_path(
    integration_workspace_config_path: str, result_server_receipts_path: Path,
    scripted_debug_backend_port: int,
) -> Iterator[str]:
    """在原工作区配置安装后、真实后端启动前登记测试 stdio Server。"""
    definitions = result_server_receipts_path.with_name("definitions.json")
    definitions.write_text(json.dumps({"probe": {"type": "object"}}), encoding="utf-8")
    config_path = Path(integration_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    config["mcp"] = {"servers": {"result": {
        "enabled": True, "transport": "stdio", "command": sys.executable,
        "args": [str(Path.cwd() / "tests/integration/workspace_services/mcp/schema_server.py"),
                 str(definitions), str(result_server_receipts_path)],
        "cwd": str(Path.cwd()),
    }}}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 目标后端反向查询 Gateway Skill 来源也必须指向本测试控制面。
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("BOXTEAM_GATEWAY_URL", f"http://127.0.0.1:{scripted_debug_backend_port + 1}")
        yield str(config_path)


@pytest.fixture
async def result_client(
    scripted_backend_process, scripted_debug_backend_port: int,
    integration_workspace_root_path: str, request: pytest.FixtureRequest,
) -> AsyncIterator[httpx.AsyncClient]:
    del scripted_backend_process
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    gateway_port = scripted_debug_backend_port + 1
    handle = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path),
        default_backend_url=f"http://127.0.0.1:{scripted_debug_backend_port}",
        port=gateway_port, artifacts_dir=context.artifacts_dir / "gateway",
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}", timeout=60,
            headers={**LOCAL_TOKEN_HEADERS, "X-Workspace-ID": workspace_backend_id_for_root(integration_workspace_root_path)},
        ) as client:
            await acquire_gateway_guest(client)
            yield client
    finally:
        close_gateway_process(handle)


async def test_product_discovery_parallel_results_and_cold_checkpoint(
    result_client, scripted_model_replay: replay_case.ScriptedModelState,
    integration_workspace_root_path: str, result_server_receipts_path: Path,
) -> None:
    client = result_client
    state = scripted_model_replay
    session = await create_session(client, "MCP 结果保真")
    response = await client.get(f"/api/v1/sessions/{session}/main-thread")
    assert response.status_code == 200, response.text
    thread = response.json()["data"]["thread_id"]
    search = replay_case._tool_call(call_index=0, name="search_tools", arguments={"tool_ids": ["mcp__result__probe"]})
    calls = [replay_case._tool_call(
        call_index=index, name="invoke_extension_tool",
        arguments={"tool_name": "mcp__result__probe", "arguments": {"result_mode": mode}},
    ) for index, mode in enumerate(("success", "error"), start=1)]
    deltas = [{"tool_calls": [search]}, {"tool_calls": calls}, {"content": "MCP_RESULT_FLOW_OK"}]
    interactions = [build_cassette(
        asset_id=f"mcp-result-{index}", model="debug-e2e-model",
        url="https://opencode.ai/zen/v1/chat/completions",
        frames=chat_completion_frames(
            response_id=f"chatcmpl-mcp-result-{index}", model="debug-e2e-model",
            deltas=({"role": "assistant", **delta},),
            finish_reason="tool_calls" if "tool_calls" in delta else "stop",
        ),
    ).raw["interactions"][0] for index, delta in enumerate(deltas)]
    state.call_ids = state.control.plan_model_calls(session, thread, interactions)
    accepted = await client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "发现 MCP probe，然后并发执行 success 与 error，读取回执后结束。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    await wait_for_job_done(client, accepted.json()["data"]["job_id"], max_attempts=120)
    records = [json.loads((state.artifacts / f"{call_id}.json").read_text()) for call_id in state.call_ids]
    assert len(records) == 3
    last_request = records[-1]["request"]
    results = [message for message in last_request["messages"] if message.get("role") == "tool"
               and message.get("tool_call_id") in {call["id"] for call in calls}]
    assert len(results) == 2
    for result in results:
        assert [block["type"] for block in result["content"]] == ["text", "text", "text"]
        assert "https://example.invalid/report" in result["content"][1]["text"]
    media = last_request["messages"][-1]
    assert media["role"] == "user"
    assert [block["type"] for block in media["content"]] == ["text", "image_url", "input_audio"] * 2
    for offset, call in zip((0, 3), calls, strict=True):
        assert call["id"] in media["content"][offset]["text"]
        assert media["content"][offset + 1]["image_url"]["url"] == f"data:image/png;base64,{RESULT_IMAGE_BASE64}"
        assert media["content"][offset + 2]["input_audio"] == {"data": RESULT_AUDIO_BASE64, "format": "wav"}
    workspace = Path(integration_workspace_root_path)
    with RolloutCheckpointSaver(workspace / ".boxteam/sessions") as cold:
        items = cold._storage.storage.read_items(session, thread)
        for record in records:
            identity = record["identity"]
            with cold._storage.storage.read_connection(session, thread) as connection:
                assembly_id, = connection.execute(
                    "SELECT assembly_id FROM context_assemblies WHERE model_call_id=?", (identity["model_call_id"],),
                ).fetchone()
            assembly = cold.get_context_assembly(session, thread, assembly_id=assembly_id)
            assert normalize_wire_request(record["request"]) == assembly.request_hash_preimage
            assembly.validate_hashes()
        with session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread):
            messages = await acheckpoint_messages(cold, await cold.aget_tuple(build_checkpoint_config(thread)))
        for index, outcome in enumerate(("success", "failure"), start=1):
            call_id = calls[index - 1]["id"]
            matching = [item for item in items if item.semantic_kind == "tool_result" and item.tool_call_id.endswith(call_id)]
            assert matching and {item.payload["tool_outcome"] for item in matching} == {outcome}
            for item in matching:
                assert [block["type"] for block in item.payload["content"]] == ["text", "image", "audio", "file", "text"]
                assert item.payload["artifact"] == {"structured_content": {"receipt": {"name": "probe", "arguments": {"result_mode": "success" if outcome == "success" else "error"}}}}
            restored = next(message for message in messages if getattr(message, "tool_call_id", None) == call_id)
            assert restored.content == matching[0].payload["content"] and restored.artifact == matching[0].payload["artifact"]
            assert restored.status == ("success" if outcome == "success" else "error")
        counts = {item.projection_identity.model_call_id for item in items if item.semantic_kind == "tool_result"
                  and item.tool_call_id.endswith(calls[0]["id"])}
        assert counts == {records[1]["identity"]["model_call_id"]}
    receipts = [json.loads(line) for line in result_server_receipts_path.read_text().splitlines()]
    assert sorted(receipt["arguments"]["result_mode"] for receipt in receipts) == ["error", "success"]


async def test_product_progress_then_disconnect_keeps_unknown_in_cold_history(
    result_client, scripted_model_replay: replay_case.ScriptedModelState,
    integration_workspace_root_path: str, result_server_receipts_path: Path,
) -> None:
    client, state = result_client, scripted_model_replay
    session = await create_session(client, "MCP 断线结果未确认")
    thread_response = await client.get(f"/api/v1/sessions/{session}/main-thread")
    assert thread_response.status_code == 200, thread_response.text
    thread = thread_response.json()["data"]["thread_id"]
    calls = [replay_case._tool_call(
        call_index=index, name="invoke_extension_tool",
        arguments={"tool_name": "mcp__result__probe", "arguments": {"call_mode": mode}},
    ) for index, mode in enumerate(("progress", "disconnect"))]
    deltas = [{"tool_calls": [calls[0]]}, {"tool_calls": [calls[1]]}, {"content": "远端结果未确认，不能自动重发。"}]
    interactions = [build_cassette(
        asset_id=f"mcp-lifecycle-{index}", model="debug-e2e-model",
        url="https://opencode.ai/zen/v1/chat/completions",
        frames=chat_completion_frames(
            response_id=f"chatcmpl-mcp-lifecycle-{index}", model="debug-e2e-model",
            deltas=({"role": "assistant", **delta},),
            finish_reason="tool_calls" if "tool_calls" in delta else "stop",
        ),
    ).raw["interactions"][0] for index, delta in enumerate(deltas)]
    state.call_ids = state.control.plan_model_calls(session, thread, interactions)
    accepted = await client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "先调用进度工具，再验证断线回执；不要重试断线调用。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    admission = accepted.json()["data"]
    await wait_for_job_done(client, admission["job_id"], max_attempts=90)
    event_response = await client.get(f"/api/v1/sessions/{session}/turns/{admission['turn_id']}/message-stream/events")
    assert event_response.status_code == 200, event_response.text
    events = event_response.json()["data"]
    progress_events = [event for event in events if event["type"] == "activity.updated"
                       and "progress" in event["payload"].get("detail", {})]
    assert [event["payload"]["detail"]["progress"] for event in progress_events] == [1, 2]
    assert {event["payload"]["detail"]["total"] for event in progress_events} == {2}
    assert "/private/" not in json.dumps(events)
    assert any(event["type"] == "activity.failed" and event["payload"]["outcome"] == "outcome_unknown" for event in events)
    records = [json.loads((state.artifacts / f"{call_id}.json").read_text()) for call_id in state.call_ids]
    last_result = next(message for message in records[-1]["request"]["messages"]
                       if message.get("role") == "tool" and message.get("tool_call_id") == calls[1]["id"])
    assert json.loads(last_result["content"])["retryable"] is False
    workspace = Path(integration_workspace_root_path)
    with RolloutCheckpointSaver(workspace / ".boxteam/sessions") as cold:
        items = cold._storage.storage.read_items(session, thread)
        results = [item for item in items if item.semantic_kind == "tool_result" and item.tool_call_id.endswith(calls[1]["id"])]
        # 现有合同分别保存 request projection 与执行 attempt，不能合并两者身份。
        assert len(results) == 2
        assert {item.projection_identity.projection_message_id is not None for item in results} == {False, True}
        assert {item.projection_identity.model_call_id for item in results} == {records[1]["identity"]["model_call_id"]}
        assert {item.payload["tool_outcome"] for item in results} == {"unknown"}
        assert {item.payload["execution_confirmed"] for item in results} == {False}
        assert results[0].payload["content"] == results[1].payload["content"]
        assert results[0].payload["artifact"] == results[1].payload["artifact"]
        receipt = McpInvocationReceipt.from_artifact(results[0].payload["artifact"])
        assert receipt is not None and receipt.effect == "unknown" and receipt.retryable is False
        with session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread):
            messages = await acheckpoint_messages(cold, await cold.aget_tuple(build_checkpoint_config(thread)))
        restored = next(message for message in messages if getattr(message, "tool_call_id", None) == calls[1]["id"])
        assert restored.content == results[0].payload["content"] and restored.artifact == results[0].payload["artifact"]
        assert restored.status == "error"
        for record in records:
            with cold._storage.storage.read_connection(session, thread) as connection:
                assembly_id, = connection.execute("SELECT assembly_id FROM context_assemblies WHERE model_call_id=?",
                                                  (record["identity"]["model_call_id"],)).fetchone()
            assembly = cold.get_context_assembly(session, thread, assembly_id=assembly_id)
            assert normalize_wire_request(record["request"]) == assembly.request_hash_preimage
            assembly.validate_hashes()
    received = [json.loads(line) for line in result_server_receipts_path.read_text().splitlines()]
    assert len([row for row in received if row["arguments"].get("call_mode") == "disconnect"]) == 1
