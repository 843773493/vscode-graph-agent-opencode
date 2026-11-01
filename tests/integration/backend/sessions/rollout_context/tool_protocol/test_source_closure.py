"""部分工具结果可读，但来源正文须等成功和失败结果全部配对后激活。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from functools import wraps
from typing import cast

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from app.agents import agent_factory
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.serialization import normalize_wire_request
from app.schemas.internal_v2.turn import TurnHistoryPageDTO
from app.services.business.session_context_resource import (
    workspace_session_inventory_uri,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _wait_until,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 使用当前文件的隔离工作区和 model_stream。
)


@dataclass
class HeldSkillReturn:
    loaded: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.fixture
def held_skill_return(monkeypatch: pytest.MonkeyPatch) -> HeldSkillReturn:
    """真实 Skill 已登记 pending 后阻塞返回，不阻塞其它工具的提交循环。"""
    hold = HeldSkillReturn()
    create = agent_factory.create_skill_load_tool

    @wraps(create)
    def gated(*args, **kwargs):
        tool = create(*args, **kwargs)
        assert isinstance(tool, StructuredTool) and tool.coroutine is not None
        invoke = tool.coroutine

        @wraps(invoke)
        async def delayed(*tool_args, **tool_kwargs):
            result = await invoke(*tool_args, **tool_kwargs)
            hold.loaded.set()
            await hold.release.wait()
            return result

        tool.coroutine = delayed
        return tool

    monkeypatch.setattr(agent_factory, "create_skill_load_tool", gated)
    return hold


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_failed_parallel_result_keeps_skill_pending_until_complete_group(
    source_case: RuntimeSourceCase, held_skill_return: HeldSkillReturn,
) -> None:
    case, hold = source_case, held_skill_return
    name, body = "closure-skill", "闭合后才能出现的 Skill 正文 Ω"
    skill = case.workspace / ".boxteam/skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\nname: {name}\ndescription: 工具因果闭包\n---\n{body}\n", encoding="utf-8")
    session, thread, _ = await _target(case)
    provider_ids = ("closure-skill-call", "closure-error-call")
    replies: list[dict[str, object]] = [{"tool_calls": [
        {"index": 0, "id": provider_ids[0], "type": "function", "function": {
            "name": "skill_load", "arguments": json.dumps({"name": name, "mode": "tracked"}),
        }},
        {"index": 1, "id": provider_ids[1], "type": "function", "function": {
            "name": "invoke_extension_tool", "arguments": json.dumps({
                "tool_name": "read_context", "arguments": {
                    "resource_identity": "/private/rejected-identity",
                    "uri": workspace_session_inventory_uri(case.container.workspace_id),
                    "scope": "workspace", "view": "inventory",
                },
            }),
        }},
    ]}, {"content": "完整配对后继续。"}]
    calls = await case.plan(session, replies, thread_id=thread)
    accepted = await _send(case, session, "并行加载来源并验证失败调用的因果边界。")
    job_id = accepted["job_id"]
    assert isinstance(job_id, str)
    saver, storage = case.container.checkpointer, case.container.checkpointer._storage.storage
    owner = ContextSourceOwnerKey(session, thread)
    failed_call = calls[0] + ":tool-call:" + provider_ids[1]
    try:
        await asyncio.wait_for(hold.loaded.wait(), 25)
        await _wait_until(lambda: any(event["phase"] == "test.tool_result.committed"
                                     and event["operation_id"] == failed_call for event in case.control.events))
        partial_items = storage.read_items(session, thread)
        result, = [item for item in partial_items if item.semantic_kind == "tool_result"]
        assert result.tool_call_id == failed_call and result.status == "completed"
        assert isinstance(result.payload, Mapping)
        assert result.payload["tool_outcome"] == "failure" and result.payload["execution_confirmed"] is True
        controls = saver.load_context_source_control_states(owner)
        pending, = [state for state in controls if state.source_kind == "skill"]
        assert pending.name == name and pending.tracking_status == "tracked"
        assert pending.latest_revision is not None and pending.latest_visible_committed_revision is None
        initial, = saver.list_context_assemblies(session, thread)
        frozen_initial = canonical_json_bytes(initial.to_dict())
        assert body not in json.dumps(case.request(calls[0]), ensure_ascii=False)
        # 公开历史读取的是实际尚未闭合的执行，不替产品构造 partial 状态。
        response = await case.client.post(f"/api/v1/sessions/{session}/threads/{thread}/history", json={
            "turn_ids": [accepted["turn_id"]], "include": ["user", "tool_call", "tool_result", "tool_summary"],
        })
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        history = TurnHistoryPageDTO.model_validate(response.json()["data"])
        turn, = history.items
        assert turn.turn_id == accepted["turn_id"] and history.thread_id == thread
        assert turn.status == "running"
        assert {tool.tool_call_id for tool in turn.tool_summary} == {
            calls[0] + ":tool-call:" + provider_id for provider_id in provider_ids
        }
        assert failed_call in response.text and body not in response.text
        assert storage.read_items(session, thread) == partial_items
        assert saver.load_context_source_control_states(owner) == controls
        assert [canonical_json_bytes(snapshot.to_dict()) for snapshot in saver.list_context_assemblies(session, thread)] == [frozen_initial]
        assert case.call_count(session, thread) == 1 and len(case.requests()) == 1
        case.write_actual("partial-tool-group", {"history": response.json(), "failed_result": result.to_dict(),
                                               "pending": asdict(pending), "assembly": initial.to_dict()})
    finally:
        hold.release.set()

    await _job(case, job_id)
    assert case.call_count(session, thread) == 2
    messages = cast(list[dict[str, object]], case.request(calls[1])["messages"])
    call_index, = [index for index, message in enumerate(messages) if message.get("tool_calls")]
    assistant_calls = cast(list[dict[str, object]], messages[call_index]["tool_calls"])
    assert [call["id"] for call in assistant_calls] == list(provider_ids)
    results = messages[call_index + 1:call_index + 3]
    assert [message["role"] for message in results] == ["tool", "tool"]
    assert [message["tool_call_id"] for message in results] == list(provider_ids)
    assert json.loads(str(results[0]["content"]))["status"] == "loaded"
    assert results[1]["content"] == "ResourceIdentity resource_identity 不得携带路径/URI/credential 形态"
    occurrences = [index for index, message in enumerate(messages) if body in json.dumps(message, ensure_ascii=False)]
    assert len(occurrences) == 1 and occurrences[0] > call_index + 2
    items = storage.read_items(session, thread)
    terminal = [item for item in items if item.semantic_kind == "tool_result"]
    attempts = [item for item in terminal if item.producer_ref["producer_kind"] == "tool"]
    projections = [item for item in terminal if item.producer_ref["producer_kind"] == "provider"]
    assert [item.tool_call_id for item in attempts] == [calls[0] + ":tool-call:" + value for value in reversed(provider_ids)]
    assert [item.tool_call_id for item in projections] == list(provider_ids)
    assert len(terminal) == 4
    for item in terminal:
        assert isinstance(item.payload, Mapping)
        assert isinstance(item.tool_call_id, str)
        assert item.status == "completed" and item.payload["execution_confirmed"] is True
        assert item.payload["tool_outcome"] == ("failure" if item.tool_call_id.endswith(provider_ids[1]) else "success")
        assert item.turn_id == accepted["turn_id"]
        assert item.projection_identity is not None and item.projection_identity.model_call_id == calls[0]
    assert case.sql(session, thread, "SELECT DISTINCT execution_id FROM item_catalog WHERE semantic_kind='tool_result'") == case.sql(
        session, thread, "SELECT execution_id FROM executions WHERE job_id=?", (job_id,),
    )
    applied, = [state for state in saver.load_context_source_control_states(owner) if state.source_kind == "skill"]
    assert applied.source_id == pending.source_id and applied.latest_revision == pending.latest_revision
    assert applied.latest_visible_committed_revision == pending.latest_revision
    snapshots = saver.list_context_assemblies(session, thread)
    assert len(snapshots) == 2 and canonical_json_bytes(snapshots[0].to_dict()) == frozen_initial
    with RolloutCheckpointSaver(case.workspace / ".boxteam/sessions") as cold:
        assert cold.load_context_source_control_states(owner) == saver.load_context_source_control_states(owner)
        for original in snapshots:
            snapshot = cold.get_context_assembly(session, thread, assembly_id=original.assembly_id)
            assert canonical_json_bytes(snapshot.to_dict()) == canonical_json_bytes(original.to_dict())
            assert isinstance(snapshot.model_call_id, str)
            assert snapshot.request_hash_preimage == normalize_wire_request(case.request(snapshot.model_call_id))
            snapshot.validate_hashes()
            projected = cold.project_context_plan_for_dispatch(session, thread, snapshot.as_sealed_plan(), target_format="chat_completions")
            assert [message.tool_call_id for message in projected.messages if isinstance(message, ToolMessage)] == (
                list(provider_ids) if snapshot.model_call_id == calls[1] else []
            )
    case.write_actual("complete-tool-group", {"calls": calls, "items": [item.to_dict() for item in terminal],
                                             "assemblies": [snapshot.to_dict() for snapshot in snapshots]})
