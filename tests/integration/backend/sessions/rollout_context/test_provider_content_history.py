"""产品执行的独立同文块在检查点和下一轮请求中保持顺序。"""

from __future__ import annotations

from typing import cast

import commentjson
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 按当前测试文件隔离工作区。
)


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
    accepted = await _send(case, session_id, "保留两段独立推理和它们之间的正文。")
    assert isinstance(accepted["job_id"], str)
    await _job(case, accepted["job_id"])
    assert case.call_count(session_id, thread_id) == 1

    with (
        RolloutCheckpointSaver(sessions_dir=case.workspace / ".boxteam/sessions") as cold,
        session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id),
    ):
        checkpoint = cold.get_tuple(cast(RunnableConfig, build_checkpoint_config(thread_id)))
        assert checkpoint is not None
        messages = checkpoint_messages(cold, checkpoint)
    assistant, = [message for message in messages if isinstance(message, AIMessage)]
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
