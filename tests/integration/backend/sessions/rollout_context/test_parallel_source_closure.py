"""真实并行 Skill 工具与团队发布必须在完整结果组之后进入模型上下文。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from functools import wraps

import pytest

from app.agents import agent_factory
from app.agents.tool_invocation_context import ToolInvocationContextMiddleware
from app.schemas.internal_v2.common import JobStatus
from app.services.business.resource_derivations.team import (
    TEAM_RESOURCE_IDS,
    TEAM_SOURCE_IDS,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _session,
    _start_job,
    runner_case,
    runtime_case,
)

__all__ = ["runner_case", "runtime_case"]


@pytest.mark.asyncio
async def test_parallel_skill_and_team_sources_wait_for_all_paired_results(
    runtime_case: RuntimeCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    container, saver = case.runner.container, case.runner.saver
    # 使用正常配置发布入口选择 model_call 边界，不替换 coordinator 或来源 owner。
    config_path = case.runner.workspace / ".boxteam" / "workspace.jsonc"
    config_path.write_text(json.dumps({"context": {"resource_activation": {
        "overrides": {"team_state": "model_call"},
    }}}), encoding="utf-8")
    assert await container.config_service.reload(idempotency_key="parallel-source-boundary")
    session, thread = await _session(case.runner)
    board = await container.team_service.create_team(
        requester_session_id=session, requester_thread_id=thread, name="并行调用团队",
    )
    await container.team_fanout_service.reconcile(session)
    names = ("parallel-first", "parallel-second")
    bodies = ("第一份冻结 Skill 正文 Ω", "第二份冻结 Skill 正文 é")
    provider_ids = ("parallel-call-first", "parallel-call-second")
    for name, body in zip(names, bodies, strict=True):
        skill = case.runner.workspace / ".boxteam" / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(f"---\nname: {name}\ndescription: 并行来源闭包\n---\n{body}\n", encoding="utf-8")

    first_loaded, second_committed, release_first = (asyncio.Event() for _ in range(3))
    create_agent = agent_factory.create_model_call_agent
    complete_result = CanonicalItemSink.complete_tool_result

    def gate_first_tool(*args, **kwargs):
        context = next(
            middleware._context for middleware in kwargs["middleware"]
            if isinstance(middleware, ToolInvocationContextMiddleware)
        )
        tool = next(tool for tool in kwargs["tools"] if tool.name == "skill_load")
        body = tool.coroutine
        assert body is not None

        @wraps(body)
        async def invoke(*body_args, **body_kwargs):
            result = await body(*body_args, **body_kwargs)
            if context.require_tool_call_id() == provider_ids[0]:
                first_loaded.set()
                await release_first.wait()
            return result

        tool.coroutine = invoke
        return create_agent(*args, **kwargs)

    async def record_result(sink, completion):
        item = await complete_result(sink, completion)
        if completion.tool_call_id.endswith(":tool-call:" + provider_ids[1]):
            second_committed.set()
        return item

    def model_reply(_payload):
        if len(case.model_requests) == 1:
            return [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                    {"index": index, "id": provider_id, "type": "function", "function": {
                        "name": "skill_load", "arguments": json.dumps({"name": name, "mode": "tracked"}),
                    }} for index, (provider_id, name) in enumerate(zip(provider_ids, names, strict=True))
                ]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "完整结果之后继续"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    monkeypatch.setattr(agent_factory, "create_model_call_agent", gate_first_tool)
    monkeypatch.setattr(CanonicalItemSink, "complete_tool_result", record_result)
    case.response_chunks = model_reply
    job = await _start_job(case.runner, "user", session, thread)
    assert job.task is not None
    try:
        await asyncio.wait_for(first_loaded.wait(), 25)
        await asyncio.wait_for(second_committed.wait(), 10)
        manager = case.reactors[0]._context_sources
        owner = ContextSourceOwnerKey(session, thread)
        pending = manager.prepare_pending()
        assert pending is not None
        skill_deltas = [delta for delta in pending.deltas if delta.source_kind == "skill"]
        assert {delta.source_name for delta in skill_deltas} == set(names)
        controls = saver.load_context_source_control_states(owner)
        skill_controls = [state for state in controls if state.source_kind == "skill"]
        assert {state.name for state in skill_controls} == set(names)
        assert all(state.tracking_status == "tracked" and state.latest_revision is not None
                   and state.latest_visible_committed_revision is None for state in skill_controls)
        original_assemblies = tuple(assembly.to_dict() for assembly in saver.list_context_assemblies(session, thread))
        assert len(original_assemblies) == len(case.model_requests) == 1
        original_items = saver._storage.storage.read_items(session, thread)
        results = [item for item in original_items if item.semantic_kind == "tool_result"]
        assert len(results) == 1 and results[0].tool_call_id.endswith(":tool-call:" + provider_ids[1])
        registry = case.resource_registries[0]
        before_revision = registry.snapshot(TEAM_RESOURCE_IDS["tasks"]).revision
        initial_frames = case.model_dispatches[0].snapshot.seal_binding.prefix.state.frames
        assert any(ref.source_identity == TEAM_SOURCE_IDS["tasks"]
                   and ref.source_revision == before_revision for frame in initial_frames for ref in frame.item_refs)

        task = await container.team_service.assign_task(
            requester_session_id=session, requester_thread_id=thread, team_id=board.team_id,
            assignee_thread_id=thread, title="未配对期间发布任务", description="只登记，不启动新执行",
            phase="review", cycle=1, depends_on_task_ids=[], start_assignee=False,
        )
        await container.team_fanout_service.reconcile(session)
        team_revision = registry.snapshot(TEAM_RESOURCE_IDS["tasks"]).revision
        assert team_revision != before_revision
        index = saver._storage.storage.index_path(session, "", thread_id=thread)
        with sqlite3.connect(index.as_uri() + "?mode=ro", uri=True) as connection:
            observations = connection.execute(
                "SELECT payload_json FROM team_source_observations WHERE session_id=? AND thread_id=? ORDER BY ledger_revision",
                (session, thread),
            ).fetchall()
        assert any(task.task.task_id in payload for (payload,) in observations)
        assert saver.load_context_source_control_states(owner) == controls
        assert manager.prepare_pending() == pending
        assert tuple(assembly.to_dict() for assembly in saver.list_context_assemblies(session, thread)) == original_assemblies
        assert saver._storage.storage.read_items(session, thread) == original_items
        assert len(case.model_requests) == 1 and not job.task.done()
        release_first.set()
        await asyncio.wait_for(asyncio.shield(job.task), 25)
    finally:
        release_first.set()
        if not job.task.done():
            job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)

    assert job.status == JobStatus.completed, job.error_message
    assert len(case.model_requests) == len(case.model_dispatches) == 2
    messages = case.model_requests[-1]["messages"]
    call_indexes = [index for index, message in enumerate(messages) if message.get("tool_calls")]
    assert len(call_indexes) == 1
    call_index = call_indexes[0]
    assert [call["id"] for call in messages[call_index]["tool_calls"]] == list(provider_ids)
    tool_results = messages[call_index + 1:call_index + 3]
    assert [message.get("role") for message in tool_results] == ["tool", "tool"]
    assert [message["tool_call_id"] for message in tool_results] == list(provider_ids)
    assert all(json.loads(message["content"])["status"] == "loaded" for message in tool_results)
    for marker in (*bodies, task.task.task_id):
        occurrences = [index for index, message in enumerate(messages)
                       if marker in json.dumps(message, ensure_ascii=False)]
        assert len(occurrences) == 1 and occurrences[0] > call_index + 2
    # 真实工具反序完成并物理提交；模型仍按原 calls 排序全部结果，再接来源。
    items = saver._storage.storage.read_items(session, thread)
    results = [item for item in items if item.semantic_kind == "tool_result"]
    streamed_results = [item for item in results if item.producer_ref["producer_kind"] == "tool"]
    model_call_id = case.model_dispatches[0].callback_model_call_id
    assert [item.tool_call_id for item in streamed_results] == [
        f"{model_call_id}:tool-call:{provider_id}" for provider_id in reversed(provider_ids)
    ]
    projection_results = [item for item in results if item.producer_ref["producer_kind"] == "provider"]
    assert [item.tool_call_id for item in projection_results] == list(provider_ids)
    assert len(results) == len(streamed_results) + len(projection_results) == 4
    final_snapshot = case.model_dispatches[-1].snapshot
    frames = final_snapshot.seal_binding.prefix.state.frames
    assert any(ref.source_identity == TEAM_SOURCE_IDS["tasks"]
               and ref.source_revision == team_revision for frame in frames for ref in frame.item_refs)
    final_controls = saver.load_context_source_control_states(owner)
    final_skills = [state for state in final_controls if state.source_kind == "skill"]
    assert {state.name for state in final_skills} == set(names)
    for state in final_skills:
        assert state.latest_visible_committed_revision == state.latest_revision
        assert any(ref.source_identity == state.source_id
                   and ref.source_revision == state.latest_revision for frame in frames for ref in frame.item_refs)
    final_pending = manager.prepare_pending()
    assert final_pending is None or not any(delta.source_kind == "skill" for delta in final_pending.deltas)
    assert saver.list_context_assemblies(session, thread)[0].to_dict() == original_assemblies[0]
