"""产品执行的独立同文块在检查点和下一轮请求中保持顺序。"""

from __future__ import annotations

import json
from typing import cast

import commentjson
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.integration.backend.sessions.fork.test_prefix_restart import _sql
from tests.integration.backend.sessions.fork.test_prefix_restart import (
    fork_deadlines as fork_deadlines,  # noqa: PLC0414 - 复用原60秒fork监督器。
)
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    _verify_frozen_history,
)
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    ModelReplayCase,
    RestartRuntimeSourceCase,
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 复用真实后端硬重启装具。
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前测试文件隔离工作区。
)
from tests.support.fork_deadline import ForkDeadlineRegistry
from tests.support.session_navigation.http_operations import submit_navigation_operation


def _plan_content(
    case: ModelReplayCase,
    session_id: str,
    thread_id: str,
    text_parts: tuple[str, str],
) -> str:
    config = commentjson.loads((case.workspace / ".boxteam/workspace.jsonc").read_text())
    endpoint = next(provider["endpoint"] for provider in config["llm"]["providers"] if provider["id"] == "primary")
    deltas = [
        {"role": "assistant", "reasoning_content": "独立的相同推理"},
        {"content": text_parts[0]},
        {"reasoning_content": "独立的相同推理"},
        {"content": text_parts[1]},
    ]
    cassette = build_cassette(
        asset_id="independent-equal-content", model="e2e-stub-model", url=endpoint + "/chat/completions",
        frames=chat_completion_frames(
            response_id="chatcmpl-independent-equal-content", model="e2e-stub-model",
            deltas=deltas, finish_reason="stop",
        ),
    )
    interaction, = cast(list[dict[str, object]], cassette.raw["interactions"])
    call, = case.control.plan_model_calls(session_id, thread_id, [interaction])
    case.planned_calls[call] = {"session_id": session_id, "thread_id": thread_id, "model_call_id": call}
    return call


def _cold_assistant(case: ModelReplayCase, session_id: str, thread_id: str) -> AIMessage:
    with (
        RolloutCheckpointSaver(sessions_dir=case.workspace / ".boxteam/sessions") as cold,
        session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id),
    ):
        checkpoint = cold.get_tuple(cast(RunnableConfig, build_checkpoint_config(thread_id)))
        assert checkpoint is not None
        messages = checkpoint_messages(cold, checkpoint)
    assistant, = [message for message in messages if isinstance(message, AIMessage)]
    return assistant


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
@pytest.mark.parametrize("text_parts", [
    ("中间文本", "最终文本"),
    (" 相同正文 ", " 相同正文 "),
], ids=["distinct-text", "equal-text-with-spaces"])
async def test_equal_reasoning_blocks_survive_product_execution_and_cold_history(
    source_case: RuntimeSourceCase,
    text_parts: tuple[str, str],
) -> None:
    case = source_case
    session_id, thread_id, _ = await _target(case)
    _plan_content(case, session_id, thread_id, text_parts)
    accepted = await _send(case, session_id, "保留两段独立推理和它们之间的正文。")
    assert isinstance(accepted["job_id"], str)
    await _job(case, accepted["job_id"])
    assert case.call_count(session_id, thread_id) == 1

    assistant = _cold_assistant(case, session_id, thread_id)
    assert isinstance(assistant.content, list)
    content = [block for block in assistant.content if isinstance(block, dict)]
    assert len(content) == len(assistant.content)
    assert [block["type"] for block in content] == ["reasoning_content", "text", "reasoning_content", "text"]
    assert [block["reasoning_content"] for block in content if block["type"] == "reasoning_content"] == [
        "独立的相同推理", "独立的相同推理",
    ]
    assert [block["text"] for block in content if block["type"] == "text"] == list(text_parts)
    frozen = case.sql(session_id, thread_id, "SELECT item_id,content_hash FROM item_catalog ORDER BY item_sequence")
    next_call, = await case.plan(session_id, [{"content": "继续处理。"}])
    successor = await _send(case, session_id, "继续并保留上一轮完整内容。")
    assert isinstance(successor["job_id"], str)
    await _job(case, successor["job_id"])
    request = case.request(next_call)
    request_messages = request["messages"]
    assert isinstance(request_messages, list)
    historical, = [message for message in request_messages if isinstance(message, dict) and message["role"] == "assistant"]
    assert historical["reasoning_content"] == "独立的相同推理\n独立的相同推理"
    assert historical["content"] == [{"type": "text", "text": text} for text in text_parts]
    assert case.sql(session_id, thread_id, "SELECT item_id,content_hash FROM item_catalog ORDER BY item_sequence")[:len(frozen)] == frozen
    assert len(case.requests()) == 2
    case.write_actual("independent-equal-content", {"content": assistant.content, "request": request, "canonical_prefix": frozen})


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_native_thinking_identity_survives_product_execution_and_cold_history(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    session, thread, _ = await _target(case)
    original: list[dict[str, object]] = [
        {"type": "thinking", "id": "native-thinking-1", "thinking": "原始推理",
         "signature": "native-signature", "provider_extension": {"trace_group": "native-source"}},
        {"type": "redacted_thinking", "id": "native-redacted-2", "data": "native-sealed-data",
         "text": "opaque-provider-metadata",
         "provider_extension": {"trace_group": "native-source"}},
    ]
    config = commentjson.loads((case.workspace / ".boxteam/workspace.jsonc").read_text())
    endpoint = next(provider["endpoint"] for provider in config["llm"]["providers"] if provider["id"] == "primary")
    cassette = build_cassette(
        asset_id="native-thinking-identity", model="e2e-stub-model", url=endpoint + "/chat/completions",
        frames=chat_completion_frames(
            response_id="chatcmpl-native-thinking-identity", model="e2e-stub-model",
            deltas=[
                # TODO: LiteLLM 1.100 的 OpenAI 流过滤器丢弃独立的 thinking_blocks
                # 后续帧（含仅签名和 redacted 帧）。上游修复后补齐这类 SDK 产品验收；
                # 当前验证首帧完整块，签名分片累计由事件消费单测覆盖。
                {"role": "assistant", "thinking_blocks": original},
                {"content": "保留原始推理身份"},
            ],
            finish_reason="stop",
        ),
    )
    interaction, = cast(list[dict[str, object]], cassette.raw["interactions"])
    call, = case.control.plan_model_calls(session, thread, [interaction])
    case.planned_calls[call] = {"session_id": session, "thread_id": thread, "model_call_id": call}
    accepted = await _send(case, session, "保留 provider 原生推理块。")
    assert isinstance(accepted["job_id"], str)
    await _job(case, accepted["job_id"])
    assistant = _cold_assistant(case, session, thread)
    assert assistant.content == [*original, {"type": "text", "text": "保留原始推理身份"}]
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    rollout = resolver.resolve_thread_node(session, thread) / "rollout"
    frozen = (rollout / "rollout.jsonl").read_bytes()
    response = await case.client.post(f"/api/v1/sessions/{session}/history", json={
        "direction": "head", "turns": 10, "include": ["user", "assistant_text", "reasoning_detail", "final_response"],
    })
    assert response.status_code == 200, response.text
    assert "native-sealed-data" not in response.text
    followup, = await case.plan(session, [{"content": "后续调用完成"}])
    continued = await _send(case, session, "继续并保持历史原件。")
    assert isinstance(continued["job_id"], str)
    await _job(case, continued["job_id"])
    assert (rollout / "rollout.jsonl").read_bytes().startswith(frozen)
    wire = [json.loads(path.read_text()) for path in sorted(case.model.artifacts.glob("*.json"))]
    _verify_frozen_history(rollout / "index.sqlite", wire)
    assert len(case.requests()) == 2
    case.write_actual("native-thinking-identity", {
        "original": original, "restored": assistant.content, "safe_history": response.json(),
        "followup_request": case.request(followup),
    })


@pytest.mark.parametrize("restart_source_case", [{"managed_gateway": True, "plugin_ids": ("terminal", "browser")}], indirect=True)
async def test_equal_blocks_survive_fork_source_deletion_and_backend_hard_restart(
    restart_source_case: RestartRuntimeSourceCase,
    fork_deadlines: ForkDeadlineRegistry,
) -> None:
    """关键状态全经消息/fork/删除入口生成，不替产品写入恢复数据。"""
    case = restart_source_case
    source_session, source_thread, _ = await _target(case)
    text_parts = (" 相同正文 ", " 相同正文 ")
    _plan_content(case, source_session, source_thread, text_parts)
    accepted = await _send(case, source_session, "保留独立同文块的来源、顺序和空白。")
    assert isinstance(accepted["job_id"], str)
    await _job(case, accepted["job_id"])
    assistant = _cold_assistant(case, source_session, source_thread)
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    source_node = resolver.resolve_session_node(source_session)
    source_rollout = resolver.resolve_thread_node(source_session, source_thread) / "rollout"
    canonical = {item["item_id"]: item for line in (source_rollout / "rollout.jsonl").read_bytes().splitlines()
                 if (item := json.loads(line))}
    assembly_query = "SELECT assembly_id,session_id,thread_id,plan_hash,request_hash,snapshot_json FROM context_assemblies ORDER BY assembly_id"
    original_assemblies = _sql(case, source_session, source_thread, assembly_query)
    assert len(original_assemblies) == 1

    async def history(session: str):
        response = await case.client.post(f"/api/v1/sessions/{session}/history", json={
            "direction": "head", "turns": 10,
            "include": ["user", "assistant_text", "reasoning_detail", "final_response"],
        })
        assert response.status_code == 200, response.text
        return response.json()["data"]["items"]

    original, = await history(source_session)
    reasoning = [part for part in original["response_parts"] if part["kind"] == "reasoning"]
    assert [part["text"] for part in reasoning] == ["独立的相同推理", "独立的相同推理"]
    assert len({part["part_id"] for part in reasoning}) == 2
    assert original["final_response"] == "".join(text_parts)
    final, = [part for part in original["response_parts"] if part["kind"] == "final_text"]
    await case.plan(source_session, [{"content": "后缀回答不得进入目标"}])
    suffix = await _send(case, source_session, "后缀问题不得进入目标")
    assert isinstance(suffix["job_id"], str)
    await _job(case, suffix["job_id"])

    forked = []
    session, thread = source_session, source_thread
    for mode in ("view_prefix_fork", "full_rollout_copy"):
        params = {"mode": mode, "thread_id": thread}
        if mode == "view_prefix_fork":
            params["item_id"] = final["source"]["item_id"]
        response = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params=params)
        assert response.status_code == 200, response.text
        point = response.json()["data"]
        key = "independent-content-" + mode
        deadline = fork_deadlines.for_operation(target=point["main_container_uri"], operation_key=key)
        response = await deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json={
            "idempotency_key": key, "source_uri": point["source_uri"], "mode": mode,
            "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
            "anchor_item_id": point["anchor_item_id"] if mode == "view_prefix_fork" else None,
            "creation_kind": "new_session_main", "target_container_uri": point["main_container_uri"],
        }), "复制并发布独立同文历史")
        assert response.status_code == 200, response.text
        deadline.complete()
        result = response.json()["data"]
        session, thread = result["session"]["session_id"], result["thread_id"]
        forked.append((session, thread))
        copied, = await history(session)
        assert copied["turn_id"] == original["turn_id"]
        assert copied["response_parts"] == original["response_parts"]
        assert copied["final_response"] == original["final_response"]
        assert _sql(case, session, thread, assembly_query) == original_assemblies
        assert case.call_count(session, thread) == 0
        assert _sql(case, session, thread, "SELECT execution_id FROM executions WHERE session_id=? AND thread_id=?", (session, thread)) == []

    intermediate_node = resolver.resolve_session_node(forked[0][0])
    # 直连Backend装具显式提供受信Gateway的actor；此场景不验证Gateway鉴权。
    case.client.headers["X-BoxTeam-Navigation-Actor"] = "user:test-user"
    for deleted in (source_session, forked[0][0]):
        receipt = await submit_navigation_operation(case.client, "delete_session", target_node_id=deleted, wait_for_settlement=True)
        assert (receipt["state"], receipt["settlement"]) == ("committed", "settled")
    assert not source_node.exists() and not intermediate_node.exists()
    leaf_rollout = resolver.resolve_thread_node(session, thread) / "rollout"
    before_restart = (leaf_rollout / "rollout.jsonl").read_bytes()
    for line in before_restart.splitlines():
        item = json.loads(line)
        assert item == canonical[item["item_id"]]
    await case.restart(hard=True)
    assert case.backends[0].process.returncode < 0
    assert (leaf_rollout / "rollout.jsonl").read_bytes() == before_restart
    restored, = await history(session)
    assert restored["response_parts"] == original["response_parts"]
    cold = _cold_assistant(case, session, thread)
    assert (cold.id, cold.content) == (assistant.id, assistant.content)
    assert _sql(case, session, thread, assembly_query) == original_assemblies

    next_call, = await case.plan(session, [{"content": "目标独立继续完成"}], thread_id=thread)
    continued = await _send(case, session, "从目标继续，保留原历史。")
    assert isinstance(continued["job_id"], str)
    await _job(case, continued["job_id"])
    request = case.request(next_call)
    request_messages = request["messages"]
    assert isinstance(request_messages, list)
    historical, = [message for message in request_messages if isinstance(message, dict) and message["role"] == "assistant"]
    assert historical["reasoning_content"] == "独立的相同推理\n独立的相同推理"
    assert historical["content"] == [{"type": "text", "text": text} for text in text_parts]
    assert "后缀" not in json.dumps(request, ensure_ascii=False)
    assert (leaf_rollout / "rollout.jsonl").read_bytes().startswith(before_restart)
    assert _sql(case, session, thread, "SELECT session_id,thread_id,model_call_id FROM model_calls WHERE session_id=? AND thread_id=?", (session, thread)) == [
        (session, thread, next_call),
    ]
    wire = [json.loads(path.read_text()) for path in sorted(case.model.artifacts.glob("*.json"))]
    _verify_frozen_history(leaf_rollout / "index.sqlite", wire)
    assert len(case.requests()) == 3
    case.write_actual("independent-content-fork-restart", {
        "original": original, "restored": restored, "forked": forked, "request": request,
        "original_assemblies": original_assemblies,
        "backend_pids": [backend.process.pid for backend in case.backends],
        "backend_exit_codes": [backend.process.poll() for backend in case.backends],
    })
