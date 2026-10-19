"""真实 checkpoint 压缩必须完整选择多工具消息组。"""

from __future__ import annotations

from copy import deepcopy

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    group_case as group_case,  # noqa: PLC0414 - 复用真实会话与执行准入 fixture
)


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("placement", ["prefix", "tail"])
@pytest.mark.parametrize("explicit_tail_id", [False, True])
def test_compaction_preserves_all_parallel_tool_group_members(
    tmp_path, group_case, frozen_provider_prepare, persisted: bool, placement: str,
    explicit_tail_id: bool,
) -> None:
    session_id, _, messages, checkpoint = group_case
    if not explicit_tail_id:
        messages[-1].id = None
    messages[1].tool_calls.append({
        "id": "call-2", "name": "search", "args": {"query": "group"}, "type": "tool_call",
    })
    messages.insert(3, ToolMessage(
        id="result-2", content="第二个工具结果", tool_call_id="call-2", name="search",
        response_metadata={"model_call_id": "model-call-1"},
    ))
    with RolloutCheckpointSaver(tmp_path) as saver:
        thread_id = saver._storage.storage.main_thread_id(session_id)
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        if persisted:
            config = saver.put(config, checkpoint, {"source": "before-compaction"}, {"messages": "1"})
        compacted = deepcopy(checkpoint)
        compacted["id"] = "compaction-complete-group"
        cutoff = 4 if placement == "prefix" else 0
        prefix = messages[:cutoff] if placement == "prefix" else []
        compacted["channel_values"]["_summarization_event"] = {
            "event_id": "group-summary", "strategy": "cache_preserving", "cutoff_index": cutoff,
            "cache_prefix_messages": prefix,
            "summary_message": HumanMessage(id="summary-group", content="保留完整工具证据"),
        }
        compacted["channel_versions"] = {"messages": "2", "_summarization_event": "2"}
        compacted["updated_channels"] = ["messages", "_summarization_event"]
        saved = saver.put(config, compacted, {"source": "compaction"}, compacted["channel_versions"])
        items = saver._storage.storage.read_items(session_id, thread_id)
        group = [item for item in items if item.message_group_id == "message-call-message-1"]
        assert [item.semantic_kind for item in group] == [
            "assistant_output", "reasoning", "assistant_output", "tool_call", "tool_call",
        ]
        original = {item.item_id: item.to_dict() for item in items}

    with RolloutCheckpointSaver(tmp_path) as cold:
        restored = cold.get_tuple(saved)
        assert restored is not None
        restored_messages = restored.checkpoint["channel_values"]["messages"]
        # 恢复会补齐 canonical manifest；核对原消息身份和业务正文，而不是
        # 把新增的权威 metadata 误当成消息变化。
        def message_facts(message, index):
            return (
                message.type, LangChainMessageCodec().message_id(message, index), message.content,
                getattr(message, "tool_calls", None), getattr(message, "tool_call_id", None),
            )

        assert [message_facts(message, index) for index, message in enumerate(restored_messages)] == [
            message_facts(message, index) for index, message in enumerate(messages)
        ]
        snapshot = frozen_provider_prepare(cold, key="after-compaction-group")
        selected = [entry.ref.ref_id for entry in snapshot.selection if entry.included]
        assert all(selected.count(item.item_id) == 1 for item in group)
        assert selected.index("item-result-1") > selected.index(group[-1].item_id)
        assert selected.index("item-result-2") > selected.index(group[-1].item_id)
        assert {item.item_id: item.to_dict() for item in cold._storage.storage.read_items(session_id, thread_id)} == original
