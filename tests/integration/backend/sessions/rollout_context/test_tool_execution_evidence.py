"""真实执行边界与延迟提交分离，历史只读取所选 view 的轻量执行证据。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from app.core.job_event_bus import EventType
from app.core.model_delta_context import (
    AgentToolExecutionCallbackHandler,
    model_tool_invocation_scope,
)
from app.core.path_utils import get_session_path_resolver, workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from app.protocol.codecs.message_stream import (
    message_stream_to_json,
    message_stream_to_proto,
)
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.catalog.tool_result_identity import (
    read_tool_result_summaries,
)
from app.services.orchestration.event_stream.processor import process_agent_event_stream
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink
from app.services.orchestration.message_stream.runtime import MessageStreamRuntime
from tests.integration.backend.sessions.rollout_context.test_turn_execution_recovery import (
    SESSION_ID,
    _accept,
    _register_provider_model_call,
    execution_scope,
)
from tests.support.rollout.view_mutations import mutate_view_members

__all__ = ["execution_scope"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["overlap", "a-before-b", "b-before-a", "cross-group-chain"])
async def test_real_tool_callbacks_keep_execution_intervals_when_consumer_is_delayed(
    tmp_path, session_bundle_factory, execution_scope, frozen_provider_activation,
    frozen_provider_wire, scenario,
) -> None:
    parallel = scenario == "overlap"
    chain = scenario == "cross-group-chain"
    sessions = tmp_path / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions)
    accepted = _accept(saver, SESSION_ID)
    model = _register_provider_model_call(
        saver, accepted, execution_scope, frozen_provider_activation, frozen_provider_wire,
        key="source-tool-boundaries",
    )
    models = {"a": model, "b": model}
    if chain:
        models["c"] = _register_provider_model_call(
            saver, accepted, execution_scope, frozen_provider_activation, frozen_provider_wire,
            key="source-tool-boundaries-next", attempt=2,
        )
    thread = saver._storage.storage.main_thread_id(SESSION_ID)
    store = MessageStreamStore(path_resolver=get_session_path_resolver(sessions))
    writer = await store.open(
        session_id=SESSION_ID, thread_id=thread, turn_id=accepted["turn_id"], job_id=accepted["job_id"],
    )
    sink = CanonicalItemSink(saver, session_id=SESSION_ID, thread_id=thread, checkpoint_ns="")
    runtime = MessageStreamRuntime(
        writer, canonical_item_sink=sink.append, tool_result_sink=sink.complete_tool_result,
        canonical_turn_id=accepted["turn_id"],
    )
    b_started, release_b, b_returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    a_completed, b_completed, c_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    body_times: dict[str, tuple[datetime, datetime]] = {}

    @tool
    async def read(path: str) -> str:
        """读取隔离装具，用同步点控制真实执行关系。"""
        start = datetime.now(UTC)
        if chain:
            if path == "a":
                await asyncio.wait_for(b_started.wait(), 5)
            elif path == "b":
                b_started.set()
                await asyncio.wait_for(c_started.wait(), 5)
            else:
                c_started.set()
                await asyncio.wait_for(b_completed.wait(), 5)
        elif parallel:
            if path == "a":
                await asyncio.wait_for(b_started.wait(), 5)
            else:
                b_started.set()
                await asyncio.wait_for(release_b.wait(), 5)
        body_times[path] = (start, datetime.now(UTC))
        if path == "b":
            b_returned.set()
        return path.upper()

    class ToolAgent:
        async def astream_events(self, input_payload, *, config, version, durability):
            assert durability == "sync"

            async def invoke_all(_input, config):
                async def invoke(path):
                    if path == "c":
                        await asyncio.wait_for(a_completed.wait(), 5)
                    with model_tool_invocation_scope(models[path], path):
                        result = await read.ainvoke({
                            "type": "tool_call", "id": path, "name": "read", "args": {"path": path},
                        }, config=config)
                    # ainvoke 已走完真实完成回调，不能只用工具正文返回作区间边界。
                    if path == "a":
                        a_completed.set()
                    elif path == "b":
                        b_completed.set()
                    return result

                if parallel or chain:
                    return await asyncio.gather(*(invoke(path) for path in models))
                execution_order = ("a", "b") if scenario == "a-before-b" else ("b", "a")
                return [await invoke(path) for path in execution_order]

            async for event in RunnableLambda(invoke_all).astream_events(input_payload, config=config, version=version):
                yield event

    async def publish(kind, payload):
        if parallel and kind == EventType.TOOL_CALL_END and payload["tool_call_id"] == "a":
            # A已返回，但消费者还未调用complete_tool。此时让B返回，证明
            # A的结束不能被消费者的这段等待推迟到B之后。
            release_b.set()
            await asyncio.wait_for(b_returned.wait(), 5)

    with execution_scope(saver, accepted), workspace_context(
        load_or_create_workspace_id(tmp_path), tmp_path,
    ):
        await runtime.start_model(model, "test-provider")
        await runtime.accept_message_chunk(AIMessageChunk(content="", tool_call_chunks=[
            {"index": i, "id": path, "name": "read", "args": '{"path":"' + path + '"}'}
            for i, path in enumerate(("a", "b"))
        ]))
        await runtime.finish_model()
        if chain:
            await runtime.start_model(models["c"], "test-provider")
            await runtime.accept_message_chunk(AIMessageChunk(content="", tool_call_chunks=[
                {"index": 0, "id": "c", "name": "read", "args": '{"path":"c"}'},
            ]))
            await runtime.finish_model()
        await process_agent_event_stream(
            agent=ToolAgent(), input_payload={}, config={}, session_id=SESSION_ID, thread_id=thread,
            job_id=accepted["job_id"], turn_id=accepted["turn_id"], agent_id="default",
            extension_skill_names_resolver=None, publish=publish,
            session_changes_service=AsyncMock(), workspace_root=tmp_path,
            message_stream_runtime=runtime,
        )

    state = await store.get_state(writer.turn_stream_id)
    snapshots = {entry["tool_call_id"].rsplit(":", 1)[-1]: entry for entry in state["tool_executions"]}
    intervals = {}
    for path, snapshot in snapshots.items():
        start = datetime.fromisoformat(snapshot["started_at"])
        end = datetime.fromisoformat(snapshot["execution_completed_at"])
        assert start <= body_times[path][0] <= body_times[path][1] <= end
        intervals[path] = (start, end)
    a, b = intervals["a"], intervals["b"]
    assert (max(a[0], b[0]) < min(a[1], b[1])) is (parallel or chain)
    if chain:
        c = intervals["c"]
        assert a[1] < c[0]
        assert max(b[0], c[0]) < min(b[1], c[1])
    elif parallel:
        assert a[1] < b[1] <= datetime.fromisoformat(snapshots["a"]["completed_at"])
    else:
        execution_order = ("a", "b") if scenario == "a-before-b" else ("b", "a")
        first, second = execution_order
        assert intervals[first][1] < intervals[second][0]

    cold = RolloutCheckpointSaver(sessions)
    history = cold.load_history(SESSION_ID, thread, TurnHistoryLoadRequest(
        turn_ids=[accepted["turn_id"]], include=["metadata", "tool_summary"],
    ))
    calls = [part for part in history.items[0].response_items if part.kind == "tool_call"]
    assert [part.tool_call_id.rsplit(":", 1)[-1] for part in calls] == list(models)
    if chain:
        a_source, b_source, c_source = (part.source for part in calls)
        assert a_source.model_call_id == b_source.model_call_id != c_source.model_call_id
        assert a_source.message_group is not None and b_source.message_group is not None
        assert a_source.message_group.message_group_id == b_source.message_group.message_group_id
        assert (a_source.message_group.ordinal, b_source.message_group.ordinal) == (0, 1)
        assert c_source.message_group is None
    for part in calls:
        path = part.tool_call_id.rsplit(":", 1)[-1]
        evidence = part.tool_execution
        assert evidence is not None and evidence.outcome == "success"
        assert (evidence.started_at, evidence.execution_completed_at) == intervals[path]
        assert (evidence.session_id, evidence.thread_id, evidence.turn_stream_id) == (
            SESSION_ID, thread, writer.turn_stream_id,
        )
    # 公开事件／snapshot往返不把源端时间换成镜像时间。
    public = message_stream_to_json(message_stream_to_proto(await writer.snapshot()))["payload"]
    assert {datetime.fromisoformat(entry["started_at"]) for entry in public["tool_executions"]} == {
        interval[0] for interval in intervals.values()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_a", [True, False])
@pytest.mark.parametrize("terminal_mode", ["complete", "interrupt", "failure"])
async def test_interleaved_tool_declarations_keep_source_order_and_individual_completeness(
    tmp_path, session_bundle_factory, execution_scope, frozen_provider_activation, frozen_provider_wire, finish_a, terminal_mode,
) -> None:
    sessions = tmp_path / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions)
    accepted = _accept(saver, SESSION_ID)
    model_call_id = _register_provider_model_call(
        saver, accepted, execution_scope, frozen_provider_activation, frozen_provider_wire,
        key="interleaved-declaration",
    )
    thread = saver._storage.storage.main_thread_id(SESSION_ID)
    store = MessageStreamStore(path_resolver=get_session_path_resolver(sessions))
    writer = await store.open(
        session_id=SESSION_ID, thread_id=thread, turn_id=accepted["turn_id"], job_id=accepted["job_id"],
    )
    sink = CanonicalItemSink(saver, session_id=SESSION_ID, thread_id=thread, checkpoint_ns="")
    runtime = MessageStreamRuntime(writer, canonical_item_sink=sink.append, canonical_turn_id=accepted["turn_id"])
    with execution_scope(saver, accepted):
        await runtime.start_model(model_call_id, "test-provider")
        # 正文index与调用index不在同一域；先收到B也不能把B声明到A前。
        await runtime.accept_message_chunk(AIMessageChunk(content=[
            {"type": "thinking", "id": "reasoning", "index": 7, "thinking": "准备读取"},
            {"type": "text", "id": "text", "index": 8, "text": "开始"},
        ]))
        for chunk in (
            {"index": 1, "id": "b", "name": "read", "args": '{"paths":["B"'},
            {"index": 0, "id": "a", "name": "read", "args": '{"paths":["A"'},
            {"index": 1, "args": ']}'},
        ):
            await runtime.accept_message_chunk(AIMessageChunk(content="", tool_call_chunks=[chunk]))
        if finish_a:
            await runtime.accept_message_chunk(AIMessageChunk(content="", tool_call_chunks=[{"index": 0, "args": ']}'}]))
        pending = runtime.pending_tool_calls()
        assert [(call_id.rsplit(":", 1)[-1], complete) for call_id, _, complete in pending] == [
            ("a", finish_a), ("b", True),
        ]
        await runtime.finish_model(
            partial=terminal_mode == "interrupt",
            completion_reason="provider_failed" if terminal_mode == "failure" else "upstream_completed",
        )

    # 冷读真实JSONL／SQLite，不以手填的source元数据冒充writer链路。
    cold = RolloutCheckpointSaver(sessions)
    items = [item for item in cold._storage.storage.read_items(SESSION_ID, thread)
             if item.semantic_kind != "user_input"]
    assert [item.semantic_kind for item in items] == ["reasoning", "assistant_output", "tool_call", "tool_call"]
    a, b = items[-2:]
    assert [item.tool_call_id.rsplit(":", 1)[-1] for item in (a, b)] == ["a", "b"]
    assert [item.status for item in (a, b)] == ["completed" if finish_a else "incomplete", "completed"]
    assert a.payload["args"] == ({"paths": ["A"]} if finish_a else '{"paths":["A"')
    assert b.payload["args"] == {"paths": ["B"]}
    assert [item.projection_identity.block_index for item in items] == [7, 8, 0, 1]
    content_status = {"complete": "completed", "interrupt": "partial", "failure": "failed"}[terminal_mode]
    assert [item.status for item in items[:2]] == [content_status] * 2
    assert len({item.message_group_id for item in items}) == 1
    assert [(item.projection_group.ordinal, item.projection_group.size) for item in items] == [
        (0, 4), (1, 4), (2, 4), (3, 4),
    ]

    history = cold.load_history(SESSION_ID, thread, TurnHistoryLoadRequest(
        turn_ids=[accepted["turn_id"]], include=["metadata", "tool_summary"],
    ))
    # 真实历史摘要的公开序列化保留组内坐标，不依赖浏览器先收到原数组。
    response = history.model_dump(mode="json")
    call_sources = {part["tool_call_id"].rsplit(":", 1)[-1]: part["source"]
                    for part in response["items"][0]["response_items"] if part["kind"] == "tool_call"}
    assert call_sources["b"]["message_group"] == {
        "message_group_id": b.message_group_id, "ordinal": 3, "size": 4,
    }
    assert call_sources["a"]["model_call_id"] == call_sources["b"]["model_call_id"] == model_call_id

    if finish_a and terminal_mode == "complete":
        codec = LangChainMessageCodec()
        message = codec.from_dict(codec.project_message(items))
        restored = codec.items_for_message(
            message, item_sequence=items[0].item_sequence, message_id=message.id,
            turn_id=accepted["turn_id"], execution_id=accepted["initial_execution_id"],
            timestamp=items[0].created_at, canonical_items=items,
        )
        assert [item.item_id for item in restored] == [item.item_id for item in items]
        assert [item.content_hash for item in restored] == [item.content_hash for item in items]
        assert [item.projection_group for item in restored] == [item.projection_group for item in items]

    snapshot = message_stream_to_json(message_stream_to_proto(await writer.snapshot()))["payload"]
    calls = {call["tool_call_id"].rsplit(":", 1)[-1]: call for call in snapshot["tool_calls"]}
    assert {key: call["provider_call_index"] for key, call in calls.items()} == {"a": 0, "b": 1}
    assert all(call["model_call_id"] == model_call_id for call in calls.values())
    assert calls["a"]["arguments_complete"] is finish_a
    assert calls["b"]["arguments"] == {"paths": ["B"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("b_outcome", ["success", "cancelled", "unknown"])
async def test_delayed_result_commit_does_not_extend_real_execution_interval(
    tmp_path, session_bundle_factory, execution_scope, frozen_provider_activation, frozen_provider_wire, monkeypatch,
    b_outcome,
) -> None:
    sessions = tmp_path / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions)
    accepted = _accept(saver, SESSION_ID)
    model_call_id = _register_provider_model_call(
        saver, accepted, execution_scope, frozen_provider_activation, frozen_provider_wire,
        key="execution-evidence",
    )
    thread = saver._storage.storage.main_thread_id(SESSION_ID)
    store = MessageStreamStore(path_resolver=get_session_path_resolver(sessions))
    writer = await store.open(
        session_id=SESSION_ID, thread_id=thread, turn_id=accepted["turn_id"], job_id=accepted["job_id"],
    )
    sink = CanonicalItemSink(saver, session_id=SESSION_ID, thread_id=thread, checkpoint_ns="")
    observed, release = asyncio.Event(), asyncio.Event()

    async def delayed_result(completion):
        if completion.tool_call_id.endswith(":tool-call:a"):
            # complete_tool 已冻结真实完成观察，持久提交与完成镜像仍未发生。
            observed.set()
            await release.wait()
        return await sink.complete_tool_result(completion)

    runtime = MessageStreamRuntime(
        writer, canonical_item_sink=sink.append, tool_result_sink=delayed_result,
        canonical_turn_id=accepted["turn_id"],
    )
    with execution_scope(saver, accepted):
        await runtime.start_model(model_call_id, "test-provider")
        await runtime.accept_message_chunk(AIMessageChunk(content="", tool_call_chunks=[
            {"index": index, "id": call, "name": "read", "args": '{"path":"fixture"}'}
            for index, call in enumerate(("a", "b"))
        ]))
        await runtime.finish_model()
        timing = AgentToolExecutionCallbackHandler(runtime)
        with model_tool_invocation_scope(model_call_id, "a"):
            timing.on_tool_start(run_id="attempt-a", tool_call_id="a")
        binding = await runtime.tool_binding_for_callback("attempt-a")
        a = binding.tool_call_id
        await runtime.start_tool(tool_execution_id="attempt-a", tool_call_id=a, tool_name="read", started_at=binding.started_at)
        timing.on_tool_end(run_id="attempt-a")
        completion = asyncio.create_task(runtime.complete_tool(
            tool_execution_id="attempt-a", tool_call_id=a, tool_name="read", status="succeeded", result="A",
        ))
        try:
            await asyncio.wait_for(observed.wait(), 5)
            with model_tool_invocation_scope(model_call_id, "b"):
                timing.on_tool_start(run_id="attempt-b", tool_call_id="b")
            binding = await runtime.tool_binding_for_callback("attempt-b")
            b = binding.tool_call_id
            await runtime.start_tool(tool_execution_id="attempt-b", tool_call_id=b, tool_name="read", started_at=binding.started_at)
            pending = await store.get_state(writer.turn_stream_id)
            assert [tool["status"] for tool in pending["tool_executions"]] == ["running", "running"]
        finally:
            release.set()
            await completion
        if b_outcome == "unknown":
            await writer.commit("interrupt.requested", {})
            await runtime.finalize_interruption_facts()
            await runtime.finalize_interruption_facts()
        else:
            timing.on_tool_end(run_id="attempt-b")
            await runtime.complete_tool(
                tool_execution_id="attempt-b", tool_call_id=b, tool_name="read", status="completed",
                outcome=b_outcome, result="B",
            )

    state = await store.get_state(writer.turn_stream_id)
    public = message_stream_to_json(message_stream_to_proto(await writer.snapshot()))["payload"]
    for original, decoded in zip(state["tool_executions"], public["tool_executions"], strict=True):
        assert decoded["outcome"] == original["outcome"]
        actual_end = original.get("execution_completed_at")
        wire_end = decoded.get("execution_completed_at")
        assert (datetime.fromisoformat(wire_end) if wire_end else None) == (
            datetime.fromisoformat(actual_end) if actual_end else None
        )
    first, second = state["tool_executions"]
    assert datetime.fromisoformat(first["execution_completed_at"]) < datetime.fromisoformat(second["started_at"])
    assert datetime.fromisoformat(first["completed_at"]) > datetime.fromisoformat(second["started_at"])
    assert first["completed_seq"] > second["started_seq"]
    assert second["outcome"] == ("outcome_unknown" if b_outcome == "unknown" else b_outcome)
    if b_outcome == "unknown":
        assert second["execution_completed_at"] is None
        assert runtime._active_tool_executions == {}
    # 下次Provider请求保留自己的结果载体；不能吞掉它，也不能将合法双载体
    # 当作两次执行。该载体没有执行区间，调用摘要仍须保留runtime的真实证据。
    with execution_scope(saver, accepted):
        request_ids = saver.ensure_request_items(
            SESSION_ID, thread, execution_id=accepted["initial_execution_id"],
            turn_id=accepted["turn_id"], messages=[ToolMessage(
                id="request-result-a", content="A", tool_call_id="a", name="read",
                response_metadata={"model_call_id": model_call_id},
            )],
        )
    assert request_ids == ("item-request-result-a",)
    # 真实 codec 往返包含执行证据的结果；manifest 不得成为第二份顶层事实。
    cold = RolloutCheckpointSaver(sessions)
    items = cold._storage.storage.read_items(SESSION_ID, thread)
    request_result = next(item for item in items if item.item_id == request_ids[0])
    assert request_result.tool_execution is None
    results = [item for item in items if item.semantic_kind == "tool_result" and item.item_id not in request_ids]
    assert len(results) == 2
    assert results[1].payload["tool_outcome"] == b_outcome
    codec = LangChainMessageCodec()
    for item in results:
        message = codec.from_dict(codec.project_message((item,)))
        restored = codec.items_for_message(
            message, item_sequence=item.item_sequence, message_id=message.id,
            turn_id=item.turn_id, execution_id=accepted["initial_execution_id"], timestamp=item.created_at,
        )
        assert len(restored) == 1
        assert restored[0].item_id == item.item_id
        assert restored[0].payload == item.payload
        assert restored[0].content_hash == item.content_hash
        assert restored[0].tool_execution == item.tool_execution

    # 冷启动后走公开历史读取入口；默认工具摘要不依赖先加载结果所在页。
    page = cold.load_history(SESSION_ID, thread, TurnHistoryLoadRequest(
        turn_ids=[accepted["turn_id"]], include=["metadata", "tool_summary"],
    ))
    response_items = page.model_dump(mode="json")["items"][0]["response_items"]
    call_parts = [part for part in response_items if part["kind"] == "tool_call"]
    result_parts = [part for part in response_items if part["kind"] == "tool_result"]
    assert [part["source"]["message_group"]["ordinal"] for part in call_parts] == [0, 1]
    assert all(part["source"]["message_group"]["size"] == 2 for part in call_parts)
    assert {part["source"]["item_id"] for part in result_parts} == {item.item_id for item in results}
    assert all(part["source"]["message_group"] is None for part in result_parts)
    calls = [item for item in items if item.semantic_kind == "tool_call"]
    call_ids = tuple(item.item_id for item in calls)
    def reject_body_read(*args, **kwargs):
        pytest.fail("工具摘要只能读取轻量声明与执行证据，不得补读canonical参数／结果正文")

    with monkeypatch.context() as patch:
        patch.setattr(cold._context_reader, "read_canonical_items", reject_body_read)
        point = cold.load_history(SESSION_ID, thread, TurnHistoryLoadRequest(
            turn_ids=[accepted["turn_id"]], item_ids=list(call_ids), include=["tool_summary"],
            source_view_id=page.source_view_id, source_view_revision=page.source_view_revision,
        ))
    summaries = point.items[0].response_items
    assert {part.source.item_id for part in summaries} == set(call_ids)
    assert all(part.kind == "tool_call" and part.projection == "summary" for part in summaries)
    assert all(part.text in ("", part.tool_name) and part.arguments is None and part.result is None
               for part in summaries)
    assert {part.tool_execution.tool_attempt_id for part in summaries} == {"attempt-a", "attempt-b"}
    assert {part.tool_execution.execution_completed_at for part in summaries} == {
        datetime.fromisoformat(first["execution_completed_at"]),
        datetime.fromisoformat(second["execution_completed_at"]) if second["execution_completed_at"] else None,
    }

    # 仅选择 call，结果在页外；直接复用生产摘要查询，不补回结果行或正文。
    with cold._storage.storage.open_read_snapshot(SESSION_ID, thread) as snapshot:
        view_id = RolloutContextReader(cold._storage).active_view_id(snapshot)
        assert view_id is not None
        evidence = read_tool_result_summaries(snapshot.connection, view_id, call_ids)
        assert {entry.tool_execution.tool_attempt_id for entry in evidence.values()} == {"attempt-a", "attempt-b"}
        assert {entry.tool_execution.execution_completed_at for entry in evidence.values()} == {
            first["execution_completed_at"], second["execution_completed_at"],
        }
        cutoff = snapshot.connection.execute(
            "SELECT max(logical_item_ordinal) FROM context_view_items WHERE view_id=? AND item_id IN (?,?)",
            (view_id, *call_ids),
        ).fetchone()[0]
        assert read_tool_result_summaries(snapshot.connection, view_id, call_ids, snapshot_upper=cutoff) == {}

    # 只给隔离查询装具排除结果，验证 reader 不从全局结果目录越过 view；不冒充 rewind 验收。
    with cold._storage.storage.write_connection(SESSION_ID, thread) as connection:
        mutate_view_members(connection,
            "UPDATE context_view_items SET visible=0 WHERE view_id=? AND item_id=?",
            (view_id, results[0].item_id))
        remaining = read_tool_result_summaries(connection, view_id, call_ids)
        assert {entry.tool_execution.tool_attempt_id for entry in remaining.values()
                if entry.tool_execution is not None} == {"attempt-b"}
        assert remaining[call_ids[0]].tool_outcome == "success"
        assert remaining[call_ids[0]].tool_execution is None
