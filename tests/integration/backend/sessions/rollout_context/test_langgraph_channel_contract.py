"""官方 DeltaChannel 与真实 SQLite Saver 的同步/异步合同；不替代产品验收。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.channels import DeltaChannel
from langgraph.checkpoint.base import writes_sort_key
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph import END, START, StateGraph, add_messages

from app.core.checkpoint_config import build_checkpoint_config
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.rollout_context.test_rollout_checkpoint_saver import (
    SESSION_ID,
    _accept_user_execution,
    _execution_scope,
    delta_history_case,
)

__all__ = ["delta_history_case"]


def _append_batches(state: list[str], writes: Sequence[list[str]]) -> list[str]:
    return [*state, *(item for batch in writes for item in batch)]


class ChannelState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    events: Annotated[list[str], DeltaChannel(_append_batches, snapshot_frequency=2)]


def _graph(saver: RolloutCheckpointSaver):
    graph = StateGraph(ChannelState)
    graph.add_node("first", lambda _: {"events": ["first"]})
    graph.add_node("second", lambda _: {"events": ["second"]})
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    return graph.compile(checkpointer=saver)


@pytest.fixture
def channel_owner(tmp_path, session_bundle_factory):
    sessions = tmp_path / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions)
    thread = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="官方增量图合同", id="channel-user")
    admission = _accept_user_execution(saver, SESSION_ID, thread, user, turn_id="channel-turn")
    return saver, admission, user, sessions


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_official_delta_graph_reopens_snapshot_and_parent_writes(channel_owner, asynchronous):
    saver, admission, user, sessions = channel_owner
    graph = _graph(saver)
    config = build_checkpoint_config(admission.thread_id)
    inputs = {"messages": [user], "events": ["root"]}
    with _execution_scope(admission):
        if asynchronous:
            result = await graph.ainvoke(inputs, config)
        else:
            result = graph.invoke(inputs, config)
        assert result["events"] == ["root", "first", "second"]
        cold = _graph(RolloutCheckpointSaver(sessions))
        if asynchronous:
            restored = await cold.aget_state(config)
            continued = await cold.ainvoke({"events": ["next"]}, config)
        else:
            restored = cold.get_state(config)
            continued = cold.invoke({"events": ["next"]}, config)
        assert restored.values["events"] == result["events"]
        assert [message.id for message in restored.values["messages"]] == [user.id]
        assert continued["events"] == ["root", "first", "second", "next", "first", "second"]
        checkpoints = list(saver.list(config))
        counters = [item.metadata.get("counters_since_delta_snapshot", {}).get("events") for item in checkpoints]
        # 官方在写入基准后清零并省略计数；不能要求latest总有非零计数。
        assert any(counter is not None and counter[0] == 1 for counter in counters)
        assert any(isinstance(item.checkpoint["channel_values"].get("events"), _DeltaSnapshot) for item in checkpoints)


async def test_parallel_write_ties_follow_official_task_path_task_id_index(delta_history_case):
    saver, admission, target, _index = delta_history_case
    middle = build_checkpoint_config(admission.thread_id, checkpoint_id="delta-middle")
    # 相同task_path时，先后提交不能取代task_id排序，也不能跨task交错相同idx。
    submitted = [("z-task", ["z0", "z1"]), ("a-task", ["a0", "a1"])]
    with _execution_scope(admission):
        for task_id, values in submitted:
            saver.put_writes(middle, [("files", value) for value in values], task_id, "same-path")
        expected = [(task, "files", value) for task, values in sorted(
            submitted, key=lambda pair: writes_sort_key("same-path", pair[0], 0)
        ) for value in values]
        cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
        sync = cold.get_tuple(middle)
        asynchronous = await cold.aget_tuple(middle)
        assert [write for write in sync.pending_writes if write[0].endswith("-task") and write[0] != "middle-task"] == expected
        assert asynchronous.pending_writes == sync.pending_writes
        history = cold.get_delta_channel_history(config=target, channels=["files"])
        assert [write for write in history["files"]["writes"] if write[0] in {"a-task", "z-task"}] == expected
        assert await cold.aget_delta_channel_history(config=target, channels=["files"]) == history
