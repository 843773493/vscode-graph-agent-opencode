"""真实图生成的 Delta 状态通过正式 fork 后仍可独立冷恢复。"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Annotated, TypedDict, cast

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels import DeltaChannel
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageRunRequest,
    RunOptions,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.job.service import JobService
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
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    runner_case,
    runtime_case,
)

__all__ = [
    "fork_context", "fork_gateway_request_context", "production_fork_container",
    "runner_case", "runtime_case", "start_model_call",
]


def _append(state, writes):
    return [*state, *(value for batch in writes for value in batch)]


class ForkMessageState(TypedDict):
    messages: Annotated[list[AnyMessage], MessagesDeltaChannel()]


class ForkDeltaState(ForkMessageState):
    events: Annotated[list[str], DeltaChannel(_append)]


def _graph(saver, admission=None, *, generic_delta=True):
    graph = StateGraph(ForkDeltaState if generic_delta else ForkMessageState)
    graph.add_node("first", lambda _: {"events": ["第一步"]} if generic_delta else {})

    def second(_):
        answer = AIMessage(id="fork-answer", content="图生成回答")
        if admission is not None:
            # 与产品执行相同，最终输出和终态先在唯一 owner 原子提交，再由图保存 checkpoint。
            StepModelCallAdapter(checkpointer=saver, session_id=admission.session_id,
                                 thread_id=admission.thread_id, job_id=admission.job_id,
                                 checkpoint_ns="").converge_final_message(answer)
        return {"messages": [answer], **({"events": ["第二步"]} if generic_delta else {})}

    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    return graph.compile(checkpointer=saver)


@pytest.mark.parametrize("mode", ["view_prefix_fork", "full_rollout_copy"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("generic_delta", [False, True])
async def test_fork_keeps_generic_delta_parent_writes_and_excludes_seed_pending(
    fork_context, production_fork_container, start_model_call, mode, asynchronous, generic_delta,
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
        graph = _graph(saver, admission, generic_delta=generic_delta)
        inputs = {"messages": [user], **({"events": ["原输入"]} if generic_delta else {})}
        actual = await graph.ainvoke(inputs, config) if asynchronous else graph.invoke(inputs, config)
        frozen = saver.get_tuple(config)
        assert "events" not in frozen.checkpoint["channel_values"]
        if generic_delta:
            saver.put_writes(frozen.config, [("events", ["尚未应用的源写入"])], "unapplied-source", "unapplied-source")
    # 关键 pending 状态由消息准入生成，fork 只复制历史，不接管来源待执行任务。
    messages = production_fork_container.message_service
    pending = await messages.create(source.session_id, MessageCreateRequest(content="只属于来源的排队任务"), thread_id=thread)
    identity = production_fork_container.job_service.reserve_user_execution_identity()
    messages.accept_user_message(pending, execution_identity=identity, queue_input=ExecutionQueueInput(
        execution_binding=identity, message_id=pending.message_id, agent_id="default", provider_id=None,
        delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=pending.created_at.isoformat(),
    ))
    assert [row.cursor.admission.job_id for row in saver.execution_queue_snapshot(source.session_id, thread).records] == [identity.job_id]
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode=mode)
    target = await context.forks.fork(request)
    assert saver.execution_queue_snapshot(target.session.session_id, target.thread_id).records == ()
    (source_pending,) = saver.execution_queue_snapshot(source.session_id, thread).records
    assert source_pending.cursor.admission.job_id == identity.job_id
    assert source_pending.phase == "queued" and source_pending.claim_generation == 0
    await context.sessions.path_resolver.delete_subtree(idempotency_key="delta-source-delete", root_node_id=source.session_id)
    cold = RolloutCheckpointSaver(context.sessions.path_resolver.sessions_root)
    owner = MutationIntentOwner(target.session.session_id, target.thread_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=target.thread_id):
        target_config = build_checkpoint_config(target.thread_id)
        cold_graph = _graph(cold, generic_delta=generic_delta)
        restored = await cold_graph.aget_state(target_config) if asynchronous else cold_graph.get_state(target_config)
        if generic_delta:
            assert restored.values["events"] == actual["events"] == ["原输入", "第一步", "第二步"]
        expected_messages = [(message.id, message.content) for message in actual["messages"]]
        if mode == "full_rollout_copy":
            # full 保留已接受的 canonical 历史，但不复制其调度权或自动执行。
            expected_messages.append((pending.message_id, pending.content))
        assert [(message.id, message.content) for message in restored.values["messages"]] == expected_messages
        target_head = cold.get_tuple(target_config)
        if generic_delta:
            assert target_head.metadata["counters_since_delta_snapshot"]["events"] == frozen.metadata["counters_since_delta_snapshot"]["events"]
        assert "messages" not in target_head.metadata["counters_since_delta_snapshot"]
        index = cold._storage.storage.root(target.session.session_id, thread_id=target.thread_id) / "index.sqlite"
        with sqlite3.connect(index) as connection:
            parent, = connection.execute("SELECT parent_checkpoint_id FROM checkpoints WHERE checkpoint_id=?", (target_head.config["configurable"]["checkpoint_id"],)).fetchone()
            assert parent == (frozen.parent_config["configurable"]["checkpoint_id"] if generic_delta else None)
            if not generic_delta:
                # 真实图的调度通道已经消费，版本仍在也不能被误判为Delta依赖。
                assert connection.execute("SELECT 1 FROM checkpoint_channels WHERE value_state='absent' "
                                          "AND channel_version IS NOT NULL AND channel_name LIKE 'branch:%' LIMIT 1").fetchone()


@pytest.mark.parametrize("anchor_mode", ["inclusive", "before"])
async def test_product_graph_history_selector_is_read_only_and_preserves_execution_head(
    runtime_case: RuntimeCase, anchor_mode: str,
) -> None:
    case = runtime_case
    container = case.runner.container
    jobs = container.job_service
    assert isinstance(jobs, JobService)
    session = await container.session_service.create(SessionCreateRequest(title="真实图历史只读入口"))
    accepted = []
    first_messages = None
    for content in ("历史第一轮", "历史第二轮"):
        result = await container.session_orchestrator.create_message(session.session_id, MessageRunRequest(
            message=MessageCreateRequest(content=content), run=RunOptions(),
        ))
        accepted.append(result)
        task = jobs._jobs[result.job_id].task
        assert task is not None
        await asyncio.wait_for(asyncio.shield(task), 20)
        assert (await jobs.get(result.job_id)).status == JobStatus.completed
        graph = case.compiled[-1][1]
        assert isinstance(graph, CompiledStateGraph)
        config = cast(RunnableConfig, build_checkpoint_config(result.thread_id))
        owner = MutationIntentOwner(session.session_id, result.thread_id)
        with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
            state = await graph.aget_state(config)
        if first_messages is None:
            first_messages = [(message.id, message.type, message.content) for message in state.values["messages"]]
    assert accepted[0].thread_id == accepted[1].thread_id
    assert [message.content for message in state.values["messages"] if isinstance(message, HumanMessage)] == [
        "历史第一轮", "历史第二轮",
    ]
    saver = case.runner.saver
    storage = saver._storage.storage
    rollout = storage.root(session.session_id, thread_id=owner.thread_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        history_config = saver.history_replay(config, turn_id=accepted[0].turn_id, anchor_mode=anchor_mode)
        assert "configurable" in history_config
        with storage._connect(owner.thread_id, session_id=session.session_id, read_only=True) as connection:
            frozen = tuple(connection.iterdump())
        frozen_jsonl = (rollout / "rollout.jsonl").read_bytes()
        cold = RolloutCheckpointSaver(storage.sessions_dir)
        expected = first_messages if anchor_mode == "inclusive" else []
        selected = cold.get_tuple(history_config)
        assert selected is not None
        snapshot = selected.checkpoint["channel_values"]["messages"]
        assert isinstance(snapshot, _DeltaSnapshot)
        assert [(message.id, message.type, message.content) for message in snapshot.value] == expected
        sync = graph.get_state(history_config)
        asynchronous = await graph.aget_state(history_config)
        for historical in (sync, asynchronous):
            assert [(message.id, message.type, message.content) for message in historical.values["messages"]] == expected
            assert "configurable" in historical.config
            assert historical.config["configurable"]["history_view_id"] == history_config["configurable"]["history_view_id"]
        replacement = {"messages": [HumanMessage(id="history-write-must-not-exist", content="禁止写入历史")]}
        with pytest.raises(ValueError, match="history-projection-read-only"):
            graph.update_state(history_config, replacement, as_node="model")
        with pytest.raises(ValueError, match="history-projection-read-only"):
            await graph.aupdate_state(history_config, replacement, as_node="model")
        with storage._connect(owner.thread_id, session_id=session.session_id, read_only=True) as connection:
            assert tuple(connection.iterdump()) == frozen
        assert (rollout / "rollout.jsonl").read_bytes() == frozen_jsonl
    assert len(case.model_requests) == 2
    successor = await container.session_orchestrator.create_message(session.session_id, MessageRunRequest(
        message=MessageCreateRequest(content="历史查看后的第三轮"), run=RunOptions(),
    ))
    task = jobs._jobs[successor.job_id].task
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), 20)
    assert (await jobs.get(successor.job_id)).status == JobStatus.completed
    assert len(case.model_requests) == 3
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        continued = await graph.aget_state(config)
    assert [message.content for message in continued.values["messages"] if isinstance(message, HumanMessage)] == [
        "历史第一轮", "历史第二轮", "历史查看后的第三轮",
    ]
    assert successor.turn_id not in {item.turn_id for item in accepted}
