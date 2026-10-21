"""真实图生成的 Delta 状态通过正式 fork 后仍可独立冷恢复。"""

from __future__ import annotations

import sqlite3
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.channels import DeltaChannel
from langgraph.graph import END, START, StateGraph

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    MessagesDeltaChannel,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from tests.integration.backend.sessions.rollout_context.test_rollout_checkpoint_saver import (
    _accept_user_execution,
    _execution_scope,
    start_model_call,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    fork_context,
    fork_gateway_request_context,
    production_fork_container,
)

__all__ = ["fork_context", "fork_gateway_request_context", "production_fork_container", "start_model_call"]


def _append(state, writes):
    return [*state, *(value for batch in writes for value in batch)]


class ForkDeltaState(TypedDict):
    messages: Annotated[list[AnyMessage], MessagesDeltaChannel()]
    events: Annotated[list[str], DeltaChannel(_append)]


def _graph(saver, admission=None):
    graph = StateGraph(ForkDeltaState)
    graph.add_node("first", lambda _: {"events": ["第一步"]})

    def second(_):
        answer = AIMessage(id="fork-answer", content="图生成回答")
        if admission is not None:
            # 与产品执行相同，最终输出和终态先在唯一 owner 原子提交，再由图保存 checkpoint。
            StepModelCallAdapter(checkpointer=saver, session_id=admission.session_id,
                                 thread_id=admission.thread_id, job_id=admission.job_id,
                                 checkpoint_ns="").converge_final_message(answer)
        return {"events": ["第二步"], "messages": [answer]}

    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    return graph.compile(checkpointer=saver)


@pytest.mark.parametrize("mode", ["view_prefix_fork", "full_rollout_copy"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_fork_keeps_generic_delta_parent_writes_and_excludes_seed_pending(
    fork_context, start_model_call, mode, asynchronous,
):
    context = fork_context
    source = await context.sessions.create(SessionCreateRequest(title="Delta fork 源"))
    saver = context.saver
    thread = saver._storage.storage.main_thread_id(source.session_id)
    user = HumanMessage(id="delta-fork-user", content="生成增量历史")
    admission = _accept_user_execution(saver, source.session_id, thread, user, turn_id="delta-fork-turn")
    config = build_checkpoint_config(thread)
    with _execution_scope(admission):
        start_model_call(saver, admission, user)
        graph = _graph(saver, admission)
        actual = await graph.ainvoke({"messages": [user], "events": ["原输入"]}, config) if asynchronous else graph.invoke({"messages": [user], "events": ["原输入"]}, config)
        frozen = saver.get_tuple(config)
        assert "events" not in frozen.checkpoint["channel_values"]
        saver.put_writes(frozen.config, [("events", ["尚未应用的源写入"])], "unapplied-source", "unapplied-source")
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode=mode)
    target = await context.forks.fork(request)
    await context.sessions.path_resolver.delete_subtree(idempotency_key="delta-source-delete", root_node_id=source.session_id)
    cold = RolloutCheckpointSaver(context.sessions.path_resolver.sessions_root)
    owner = MutationIntentOwner(target.session.session_id, target.thread_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=target.thread_id):
        target_config = build_checkpoint_config(target.thread_id)
        restored = await _graph(cold).aget_state(target_config) if asynchronous else _graph(cold).get_state(target_config)
        assert restored.values["events"] == actual["events"] == ["原输入", "第一步", "第二步"]
        assert [(message.id, message.content) for message in restored.values["messages"]] == [(message.id, message.content) for message in actual["messages"]]
        target_head = cold.get_tuple(target_config)
        assert target_head.metadata["counters_since_delta_snapshot"]["events"] == frozen.metadata["counters_since_delta_snapshot"]["events"]
        assert "messages" not in target_head.metadata["counters_since_delta_snapshot"]
        index = cold._storage.storage.root(target.session.session_id, thread_id=target.thread_id) / "index.sqlite"
        with sqlite3.connect(index) as connection:
            assert connection.execute("SELECT parent_checkpoint_id FROM checkpoints WHERE checkpoint_id=?", (target_head.config["configurable"]["checkpoint_id"],)).fetchone()[0] == frozen.parent_config["configurable"]["checkpoint_id"]
