"""真实 Runner 发现、压缩、冷读与 fork；不替产品写入关键历史。"""

from __future__ import annotations

import asyncio
import json

from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import MessageReplayRequest
from app.schemas.internal_v2.session import SessionForkRequest
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.tool_definitions import (
    definition_source_values,
)
from app.testing.model_stream.assets import chat_completion_frames
from tests.integration.backend.sessions import test_thread_runtime_runner as runner

runner_case = runner.runner_case
runtime_case = runner.runtime_case
runner_gateway_context = runner.runner_gateway_context
runner_compaction_gateway_context = runner.runner_compaction_gateway_context
gateway_skill_publication = runner.gateway_skill_publication


def _reply(content: str, *, tool: str | None = None, arguments: dict[str, object] | None = None):
    delta = {"role": "assistant", "content": content} if tool is None else {
        "role": "assistant", "tool_calls": [{"index": 0, "id": "definition-" + tool, "type": "function",
        "function": {"name": tool, "arguments": json.dumps(arguments or {})}}],
    }
    frames = chat_completion_frames(
        response_id="definition-response", model="runtime-model", deltas=[delta],
        finish_reason="stop" if tool is None else "tool_calls",
    )
    chunks: list[dict[str, object]] = []
    for frame in frames:
        if frame.kind == "data":
            assert isinstance(frame.payload, dict)
            chunks.append(frame.payload)
    tokens = 90000 if tool == "compact_conversation" else 100
    chunks[-1]["usage"] = {"prompt_tokens": tokens - 1, "completion_tokens": 1, "total_tokens": tokens}
    return chunks


async def test_discovered_definition_survives_real_compaction_cold_read_and_fork(
    runtime_case, runner_compaction_gateway_context, gateway_skill_publication, fork_deadlines,
):
    case = runtime_case
    session, thread = await runner._session(case.runner)
    phase = "history"
    searched = False
    compressed = False
    target = "list_debug_configurations"

    def reply(payload):
        nonlocal searched, compressed
        if case.model_dispatches[-1].snapshot.selection_policy == "summary_candidate":
            return _reply("摘要：此前已发现调试工具并完成历史记录。")
        if phase == "discover" and not searched:
            searched = True
            return _reply("", tool="search_tools", arguments={"tool_ids": [target]})
        if phase == "compact" and not compressed:
            compressed = True
            return _reply("", tool="compact_conversation")
        return _reply("历史记录。" * 1000 if phase != "successor" else "恢复后继续。")

    case.response_chunks = reply
    for _ in range(2):
        await runner._complete_job(case, session, thread, timeout_seconds=25)
    phase = "discover"
    discovery_admission = await runner._complete_job(case, session, thread, timeout_seconds=35)
    assert not tuple(definition_source_values(case.model_dispatches[-1].snapshot))
    result, = [message for message in case.model_requests[-1]["messages"]
               if message.get("role") == "tool" and message.get("tool_call_id") == "definition-search_tools"]
    entry, = json.loads(result["content"])["results"]
    assert entry["tool_id"] == target and entry["schema_state"] == "inline_complete"
    original = entry["provenance"]
    phase = "history"
    for _ in range(2):
        await runner._complete_job(case, session, thread, timeout_seconds=25)
    phase = "compact"
    await runner._complete_job(case, session, thread, timeout_seconds=40)
    snapshot = case.model_dispatches[-1].snapshot
    assert snapshot.seal_binding.prefix.state.epoch.reason.value == "compaction"
    restored, = [value for value, _ in definition_source_values(snapshot) if value.status == "available"]
    assert restored.tool_id == target
    assert restored.definition is not None
    assert restored.definition.input_schema == entry["input_schema"]
    assert restored.definition.provenance.model_dump() == original
    assert restored.discovered_from.model_call_id
    assert not any(message.get("role") == "tool" and message.get("tool_call_id") == "definition-search_tools"
                   for message in case.model_requests[-1]["messages"])
    cold = RolloutCheckpointSaver(case.runner.workspace / ".boxteam/sessions")
    assert cold.get_context_assembly(session, thread, assembly_id=snapshot.assembly_id).to_dict() == snapshot.to_dict()
    phase = "successor"
    await runner._complete_job(case, session, thread, timeout_seconds=25)
    assert len(tuple(definition_source_values(case.model_dispatches[-1].snapshot))) == 1
    forks = case.runner.container.session_context_fork_service
    point = await forks.source_point(session_id=session, thread_id=thread, mode="last_completed_turn_fork")
    request = SessionForkRequest(
        idempotency_key="discovered-definition-fork", source_uri=point.source_uri,
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
        mode="last_completed_turn_fork", creation_kind="new_child_thread", target_container_uri=point.child_container_uri,
    )
    deadline = fork_deadlines.for_operation(
        target=f"{case.runner.container.workspace_service.workspace_id}/{request.target_container_uri}",
        operation_key=request.idempotency_key,
    )
    forked = await deadline.wait(forks.fork(request), "SessionContextForkService.fork")
    fork_deadlines.complete(deadline)
    await runner._complete_job(case, session, forked.thread_id, timeout_seconds=35)
    child, = [value for value, _ in definition_source_values(case.model_dispatches[-1].snapshot)]
    assert child == restored
    assert case.model_dispatches[-1].snapshot.thread_id == forked.thread_id
    # 从正式编辑入口回退到发现之前，不能让后来的活动定义重新进入请求。
    assert discovery_admission.turn_id is not None
    replayed = await case.runner.container.session_turn_replay_service.replay_turn(
        session, discovery_admission.turn_id,
        MessageReplayRequest(
            action="edit_and_continue", content="从此处重新继续。", acknowledge_context_only=True,
        ),
    )

    async def wait_replay():
        while True:
            job = await case.runner.container.job_service.get(replayed.job_id)
            if job.status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
                assert job.status == JobStatus.completed, job.error_message
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait_replay(), 35)
    rewound = case.model_dispatches[-1].snapshot
    assert rewound.thread_id == thread
    assert rewound.seal_binding.prefix.state.epoch.reason.value == "rewind"
    assert not tuple(definition_source_values(rewound))
    assert not any(message.get("role") == "tool" and message.get("tool_call_id") == "definition-search_tools"
                   for message in case.model_requests[-1]["messages"])
