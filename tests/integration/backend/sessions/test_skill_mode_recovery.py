"""由真实执行生成 Skill 模式与检查点，再经正式编辑入口恢复。"""

from __future__ import annotations

import asyncio
import json
from typing import cast

from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import MessageReplayRequest
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.testing.model_stream.assets import chat_completion_frames
from tests.integration.backend.sessions import test_thread_runtime_runner as runner

runner_case = runner.runner_case
runtime_case = runner.runtime_case
runner_gateway_context = runner.runner_gateway_context
gateway_skill_publication = runner.gateway_skill_publication


async def test_product_skill_modes_restore_the_selected_checkpoint(
    runtime_case: runner.RuntimeCase, runner_gateway_context, gateway_skill_publication,
):
    case = runtime_case
    session, thread = await runner._session(case.runner)
    owner = ContextSourceOwnerKey(session, thread)
    name = "mode-recovery"
    body = "模式恢复验收的唯一正文 Ω\n"
    skill = case.runner.workspace / ".boxteam/skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\nname: {name}\ndescription: 模式恢复\n---\n{body}", encoding="utf-8")
    phase = "initial"
    mode = "tracked"
    called = False
    replaying = False

    def reply(_payload: dict[str, object]) -> list[dict[str, object]]:
        nonlocal called
        delta: dict[str, object]
        if not replaying and not called:
            called = True
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "mode-" + phase, "type": "function",
                "function": {"name": "skill_load", "arguments": json.dumps({"name": name, "mode": mode})},
            }]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "完成 " + phase}
            finish = "stop"
        frames = chat_completion_frames(
            response_id="mode-response-" + phase, model="runtime-model", deltas=[delta], finish_reason=finish,
        )
        chunks = []
        for frame in frames:
            if frame.kind == "data":
                assert isinstance(frame.payload, dict)
                chunks.append(frame.payload)
        return chunks

    case.response_chunks = reply
    saver = case.runner.saver
    admissions = []
    originals = []
    initial_member = None

    def control():
        state, = [state for state in saver.load_context_source_control_states(owner)
                  if state.source_kind == "skill" and state.name == name]
        return state

    def skill_members():
        snapshot = case.model_dispatches[-1].snapshot
        assert snapshot.seal_binding is not None
        return [(ref, block) for frame in snapshot.seal_binding.prefix.state.frames
                if frame.role == "user" and frame.source_protocol is not None
                for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
                if block.name == name]

    for index, mode in enumerate(("tracked", "snapshot", "tracked", "untrack")):
        phase, called = str(index), False
        admissions.append(await runner._complete_job(case, session, thread, timeout_seconds=35))
        messages = cast(list[dict[str, object]], case.model_requests[-1]["messages"])
        result, = [message for message in messages if message.get("role") == "tool"
                   and message.get("tool_call_id") == "mode-" + phase]
        assert isinstance(result["content"], str)
        receipt = json.loads(result["content"])
        assert receipt["error"] is None and receipt["mode"] == mode
        assert receipt["tracked"] is (mode == "tracked")
        state = control()
        assert state.tracking_status == ("tracked" if mode == "tracked" else "untracked")
        assert state.latest_visible_committed_revision == receipt["revision"]
        member, = skill_members()
        assert member[1].body == body
        assert member[0].source_tracking_at_seal == "tracked"
        if initial_member is None:
            initial_member = member
            assert receipt["queued"] is True and receipt["append_status"] == "appended"
        else:
            assert member == initial_member
            assert receipt["queued"] is False
            if mode != "untrack":
                assert receipt["append_status"] == "already_active"
        snapshot = case.model_dispatches[-1].snapshot
        originals.append((snapshot.assembly_id, canonical_json_bytes(snapshot.to_dict())))

    # 回退前的状态均由上面的真实 ToolNode 执行及正常 checkpoint writer 创建。
    assert initial_member is not None
    replaying = True

    async def await_replay(job_id: str) -> None:
        while True:
            job = await case.runner.container.job_service.get(job_id)
            if job.status in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}:
                assert job.status == JobStatus.completed, job.error_message
                return
            await asyncio.sleep(0.02)

    for index, expected in ((2, "untracked"), (1, "tracked")):
        phase = "rewind-" + str(index)
        turn_id = admissions[index].turn_id
        assert turn_id is not None
        accepted = await case.runner.container.session_turn_replay_service.replay_turn(
            session, turn_id,
            MessageReplayRequest(action="edit_and_continue", content=phase, acknowledge_context_only=True),
        )

        await asyncio.wait_for(await_replay(accepted.job_id), 35)
        assert control().tracking_status == expected
        snapshot = case.model_dispatches[-1].snapshot
        assert snapshot.seal_binding is not None
        assert snapshot.seal_binding.prefix.state.epoch.reason.value == "rewind"
        member, = skill_members()
        assert member[0].source_revision == initial_member[0].source_revision
        assert member[0].source_tracking_at_seal == expected
        assert member[1].body == body
        with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
            assert cold.load_context_source_control_states(owner) == saver.load_context_source_control_states(owner)
            for assembly_id, frozen in originals:
                restored = cold.get_context_assembly(session, thread, assembly_id=assembly_id)
                restored.validate_hashes()
                assert canonical_json_bytes(restored.to_dict()) == frozen
