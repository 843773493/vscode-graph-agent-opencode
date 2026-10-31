"""由真实执行生成 Skill 模式与检查点，再经正式编辑入口恢复。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api.deps import get_session_skill_tracking_service, verify_local_token
from app.api.sessions import router
from app.core.trace_middleware import TraceMiddleware
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
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    SkillCatalogActivationSnapshot,
    SkillLoadMode,
    SkillLoadReceipt,
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


@pytest.fixture
async def untrack_http(runtime_case: runner.RuntimeCase):
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_session_skill_tracking_service] = lambda: runtime_case.runner.container.session_skill_tracking_service
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application, raise_app_exceptions=False), base_url="http://test") as client:
        yield client


async def test_product_untrack_rejects_bad_response_and_retains_committed_history(
    runtime_case: runner.RuntimeCase, runner_gateway_context, gateway_skill_publication,
    untrack_http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case = runtime_case
    session_id, thread_id = await runner._session(case.runner)
    owner = ContextSourceOwnerKey(session_id, thread_id)
    name = "untrack-response"
    body = "必须保留的原始 Skill 正文 Ω\n"
    source = case.runner.workspace / ".boxteam/skills" / name / "SKILL.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"---\nname: {name}\ndescription: 响应地址验收\n---\n{body}", encoding="utf-8")
    loaded = False

    def reply(_payload: dict[str, object]) -> list[dict[str, object]]:
        nonlocal loaded
        if not loaded:
            loaded = True
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "load-before-untrack", "type": "function",
                "function": {"name": "skill_load", "arguments": json.dumps({"name": name, "mode": "tracked"})},
            }]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "跟踪已建立"}
            finish = "stop"
        return [frame.payload for frame in chat_completion_frames(
            response_id="untrack-response", model="runtime-model", deltas=[delta], finish_reason=finish,
        ) if frame.kind == "data" and isinstance(frame.payload, dict)]

    case.response_chunks = reply
    await runner._complete_job(case, session_id, thread_id, timeout_seconds=35)
    messages = cast(list[dict[str, object]], case.model_requests[-1]["messages"])
    receipts = []
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == "load-before-untrack":
            content = message["content"]
            assert isinstance(content, str)
            receipts.append(json.loads(content))
    assert len(receipts) == 1 and receipts[0]["tracked"] is True and receipts[0]["error"] is None
    saver = case.runner.saver
    before, = [state for state in saver.load_context_source_control_states(owner) if state.source_kind == "skill" and state.name == name]
    assert before.tracking_status == "tracked"
    snapshot = case.model_dispatches[-1].snapshot
    frozen = canonical_json_bytes(snapshot.to_dict())
    url = f"/api/v1/sessions/{session_id}/threads/{thread_id}/skills/untrack"
    original = ContextSourceManager.load_skill
    statuses: list[str] = []
    bad_addresses = ["/private/skills/SKILL.md", r"C:\private\skills\SKILL.md", r"\\host\share\SKILL.md",
                     "skills/SKILL.md", "file:///private/skills/SKILL.md", ""]

    for index, bad_address in enumerate(bad_addresses):
        def corrupt_receipt(manager: ContextSourceManager, name: str, mode: SkillLoadMode = "snapshot", *,
                            activation_snapshot: SkillCatalogActivationSnapshot | None = None,
                            _bad_address: str = bad_address) -> SkillLoadReceipt:
            receipt = original(manager, name, mode, activation_snapshot=activation_snapshot)
            assert mode == "untrack"
            statuses.append(receipt.status)
            return replace(receipt, display_uri=_bad_address)

        request_id = f"request-untrack-bad-{index}"
        with monkeypatch.context() as patch:
            # 仅破坏已完成原 mutation 后的内部回执，不替产品构造 tracking/checkpoint。
            patch.setattr(ContextSourceManager, "load_skill", corrupt_receipt)
            response = await untrack_http.post(url, json={"name": name}, headers={"X-Request-ID": request_id})
        assert response.status_code == 500, response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
        assert response.json()["data"] is None and "display_uri" not in response.text
        assert not bad_address or bad_address not in response.text
        errors = [record.exc_info[1] for record in caplog.records if record.exc_info
                  and isinstance(record.exc_info[1], ValidationError)]
        assert any(issue["input"] == bad_address and issue["loc"] == ("display_uri",)
                   for error in errors for issue in error.errors())
        after, = [state for state in saver.load_context_source_control_states(owner) if state.source_kind == "skill" and state.name == name]
        assert after.tracking_status == "untracked"
        assert after.state_revision == before.state_revision + 1

    assert statuses == ["loaded", *("not_tracked" for _ in bad_addresses[1:])]
    recovered = await untrack_http.post(url, json={"name": name}, headers={"X-Request-ID": "request-untrack-restored"})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["data"]["status"] == "not_tracked"
    assert recovered.json()["data"]["display_uri"] is None
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        assert cold.load_context_source_control_states(owner) == saver.load_context_source_control_states(owner)
        restored = cold.get_context_assembly(session_id, thread_id, assembly_id=snapshot.assembly_id)
        restored.validate_hashes()
        assert canonical_json_bytes(restored.to_dict()) == frozen
