"""官方 messages Delta 的真实图执行、同步异步和冻结历史恢复。"""

from __future__ import annotations

import sqlite3
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    RemoveMessage,
    ToolMessage,
)
from langgraph.channels import DeltaChannel
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Overwrite

from app.agents.context_checkpoint_store import ContextCompactionCheckpointStore
from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    MessagesDeltaChannel,
    acheckpoint_messages,
    checkpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.rollout_context.test_langgraph_channel_contract import (
    channel_owner,
)
from tests.integration.backend.sessions.rollout_context.test_rollout_checkpoint_saver import (
    _execution_scope,
)

__all__ = ["channel_owner"]


class MessageState(TypedDict):
    messages: Annotated[list[AnyMessage], MessagesDeltaChannel(snapshot_frequency=2)]


def _graph(saver, mutation):
    graph = StateGraph(MessageState)
    graph.add_node("first", lambda _: {"messages": [AIMessage(id="first", content="第一步")]})
    graph.add_node("second", lambda _: {"messages": mutation})
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    return graph.compile(checkpointer=saver)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mutation", ["append", "remove", "clear", "overwrite"])
async def test_message_delta_cold_graph_and_business_reader_match(channel_owner, asynchronous, mutation):
    saver, admission, user, sessions = channel_owner
    values = {
        "append": [AIMessage(id="second", content="第二步")],
        "remove": [RemoveMessage(id="first")],
        "clear": [RemoveMessage(id=REMOVE_ALL_MESSAGES), AIMessage(id="second", content="清空后新增")],
        "overwrite": Overwrite([AIMessage(id="second", content="仅保留第二步")]),
    }
    graph = _graph(saver, values[mutation])
    config = build_checkpoint_config(admission.thread_id)
    with _execution_scope(admission):
        actual = await graph.ainvoke({"messages": [user]}, config) if asynchronous else graph.invoke({"messages": [user]}, config)
        frozen = saver.get_tuple(config)
        fixed = saver.get_tuple(frozen.config)
        if mutation == "overwrite":
            # 官方 Overwrite 强制建立新基准，不受常规更新计数限制。
            assert isinstance(fixed.checkpoint["channel_values"]["messages"], _DeltaSnapshot)
        else:
            assert "messages" not in fixed.checkpoint["channel_values"]
        cold_saver = RolloutCheckpointSaver(sessions)
        cold_graph = _graph(cold_saver, values[mutation])
        restored = await cold_graph.aget_state(frozen.config) if asynchronous else cold_graph.get_state(frozen.config)
        expected = [(message.id, message.content) for message in actual["messages"]]
        assert [(message.id, message.content) for message in restored.values["messages"]] == expected
        assert [(message.id, message.content) for message in checkpoint_messages(cold_saver, fixed)] == expected

        assert [(message.id, message.content) for message in await acheckpoint_messages(cold_saver, fixed)] == expected
        checkpoints = list(saver.list(config))
        assert any(isinstance(item.checkpoint["channel_values"].get("messages"), _DeltaSnapshot) for item in checkpoints)
        assert any(item.metadata.get("counters_since_delta_snapshot", {}).get("messages") for item in checkpoints)
        # 目标 pending 不提前参与已提交状态；按 ID 读取仍保持相同结果。
        saver.put_writes(frozen.config, [("messages", [AIMessage(id="pending", content="尚未执行")])], "unapplied", "unapplied-path")
        assert [(message.id, message.content) for message in checkpoint_messages(cold_saver, fixed)] == expected

@pytest.mark.parametrize("mutation", [
    {"role": "assistant", "content": "原生字典输入"},
    ("assistant", "原生元组输入"),
    AIMessageChunk(content="流式分片输入"),
])
async def test_raw_message_like_gets_stable_id_before_write_and_cold_replay(channel_owner, mutation):
    saver, admission, user, sessions = channel_owner
    graph = _graph(saver, mutation)
    config = build_checkpoint_config(admission.thread_id)
    with _execution_scope(admission):
        result = await graph.ainvoke({"messages": [user]}, config)
        expected = [(message.id, message.type, message.content) for message in result["messages"]]
        assert all(message_id for message_id, _, _ in expected)
        assert all(not isinstance(message, AIMessageChunk) for message in result["messages"])
        cold = RolloutCheckpointSaver(sessions)
        fixed = saver.get_tuple(config).config
        for _ in range(2):
            restored = await cold.aget_tuple(fixed)
            assert [(message.id, message.type, message.content) for message in await acheckpoint_messages(cold, restored)] == expected


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_conditional_edge_reads_tool_message_before_pending_write(channel_owner, asynchronous):
    saver, admission, user, sessions = channel_owner
    output = ToolMessage(content="条件边前的工具结果", tool_call_id="fresh-read-call")
    observed = []
    graph = StateGraph(MessageState)
    graph.add_node("tool", lambda _: {"messages": [output]})

    def route(state):
        observed.append(state["messages"][-1].id)
        return END

    graph.add_edge(START, "tool")
    graph.add_conditional_edges("tool", route)
    compiled = graph.compile(checkpointer=saver)
    config = build_checkpoint_config(admission.thread_id)
    with _execution_scope(admission):
        actual = await compiled.ainvoke({"messages": [user]}, config) if asynchronous else compiled.invoke({"messages": [user]}, config)
        frozen = saver.get_tuple(config)
        cold = RolloutCheckpointSaver(sessions)
        restored = await acheckpoint_messages(cold, await cold.aget_tuple(frozen.config))
    assert observed == [output.id]
    assert output.id
    assert [message.id for message in restored] == [message.id for message in actual["messages"]]


@pytest.mark.parametrize("mutation", [
    {"role": "assistant", "content": "条件边字典"},
    ("assistant", "条件边元组"),
])
async def test_fresh_message_like_does_not_invent_unpersisted_identity(channel_owner, mutation):
    saver, admission, user, sessions = channel_owner
    graph = StateGraph(MessageState)
    graph.add_node("emit", lambda _: {"messages": mutation})
    observed = []

    def route(state):
        observed.append(state["messages"][-1].id)
        return END

    graph.add_edge(START, "emit")
    graph.add_conditional_edges("emit", route)
    config = build_checkpoint_config(admission.thread_id)
    with _execution_scope(admission):
        result = await graph.compile(checkpointer=saver).ainvoke({"messages": [user]}, config)
        fixed = saver.get_tuple(config).config
        cold = RolloutCheckpointSaver(sessions)
        restored = await acheckpoint_messages(cold, await cold.aget_tuple(fixed))
    assert observed == [None]
    assert result["messages"][-1].id
    assert [message.id for message in restored] == [message.id for message in result["messages"]]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("raw", [
    {"role": "assistant", "content": "删除同批字典"},
    ("assistant", "删除同批元组"),
])
async def test_fresh_raw_message_with_remove_has_no_temporary_identity(channel_owner, asynchronous, raw):
    saver, admission, user, sessions = channel_owner
    graph = StateGraph(MessageState)
    graph.add_node("emit", lambda _: {"messages": [raw, RemoveMessage(id=user.id)]})
    observed = []

    def route(state):
        observed.append([(message.id, message.content) for message in state["messages"]])
        return END

    graph.add_edge(START, "emit")
    graph.add_conditional_edges("emit", route)
    config = build_checkpoint_config(admission.thread_id)
    compiled = graph.compile(checkpointer=saver)
    with _execution_scope(admission):
        result = await compiled.ainvoke({"messages": [user]}, config) if asynchronous else compiled.invoke({"messages": [user]}, config)
        expected = [(message.id, message.content) for message in result["messages"]]
        cold = RolloutCheckpointSaver(sessions)
        restored = await acheckpoint_messages(cold, await cold.aget_tuple(saver.get_tuple(config).config))
    assert len(expected) == 1 and expected[0][0]
    assert observed == [[(None, expected[0][1])]]
    assert [(message.id, message.content) for message in restored] == expected


async def test_unknown_remove_fails_at_graph_entry(channel_owner):
    saver, admission, user, _sessions = channel_owner
    with _execution_scope(admission), pytest.raises(ValueError, match="doesn't exist"):
        await _graph(saver, [RemoveMessage(id="unknown")]).ainvoke(
            {"messages": [user]}, build_checkpoint_config(admission.thread_id),
        )


async def test_manual_message_baseline_keeps_other_channel_replay_counters(channel_owner):
    saver, admission, user, sessions = channel_owner

    class State(TypedDict):
        messages: Annotated[list[AnyMessage], MessagesDeltaChannel()]
        events: Annotated[list[str], DeltaChannel(lambda state, writes: [*state, *(entry for batch in writes for entry in batch)])]

    graph = StateGraph(State)
    graph.add_node("emit", lambda _: {"events": ["事件"], "messages": [AIMessage(id="answer", content="正文")]})
    graph.add_edge(START, "emit")
    graph.add_edge("emit", END)
    config = build_checkpoint_config(admission.thread_id)
    with _execution_scope(admission):
        compiled = graph.compile(checkpointer=saver)
        actual = await compiled.ainvoke({"messages": [user], "events": ["输入"]}, config)
        before = saver.get_tuple(config)
        store = ContextCompactionCheckpointStore(checkpointer=saver)
        loaded = await store.load(MutationIntentOwner(admission.session_id, admission.thread_id))
        await store.save_compaction_request(checkpoint=loaded)
        after = saver.get_tuple(config)
        assert after.metadata["counters_since_delta_snapshot"]["events"] == before.metadata["counters_since_delta_snapshot"]["events"]
        assert "messages" not in after.metadata["counters_since_delta_snapshot"]
        assert after.checkpoint["channel_versions"]["messages"] == before.checkpoint["channel_versions"]["messages"]
        assert isinstance(saver.get_tuple(after.config).checkpoint["channel_values"]["messages"], _DeltaSnapshot)
        cold_graph = graph.compile(checkpointer=RolloutCheckpointSaver(sessions))
        restored = await cold_graph.aget_state(after.config)
        assert restored.values["events"] == actual["events"]
        assert [(message.id, message.content) for message in restored.values["messages"]] == [(message.id, message.content) for message in actual["messages"]]


@pytest.mark.parametrize("corruption", ["missing-parent", "versioned-root"])
def test_delta_corruption_fails_for_frozen_latest_and_history_reads(channel_owner, corruption):
    saver, admission, user, sessions = channel_owner
    config = build_checkpoint_config(admission.thread_id)
    with _execution_scope(admission):
        _graph(saver, [AIMessage(id="second", content="第二步")]).invoke({"messages": [user]}, config)
        frozen = saver.get_tuple(config).config
        index = saver._storage.storage.root(admission.session_id, thread_id=admission.thread_id) / "index.sqlite"
        with sqlite3.connect(index) as connection:
            if corruption == "missing-parent":
                connection.execute("UPDATE checkpoints SET parent_checkpoint_id='missing' WHERE checkpoint_id=?", (frozen["configurable"]["checkpoint_id"],))
            else:
                connection.execute("UPDATE checkpoints SET parent_checkpoint_id=NULL WHERE checkpoint_id=?", (frozen["configurable"]["checkpoint_id"],))
        cold = RolloutCheckpointSaver(sessions)
        for selector in (config, frozen):
            with pytest.raises(RuntimeError, match="缺少已引用的祖先|缺少基准"):
                cold.get_tuple(selector)
        with pytest.raises(RuntimeError, match="缺少已引用的祖先|缺少基准"):
            cold.get_delta_channel_history(config=frozen, channels=["messages"])
