"""真实配置发布与消息入口验证activation策略只在新Turn生效。"""

from __future__ import annotations

import asyncio
import json

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from configs.runtime import read_jsonc_object
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 — 原产品装具按当前文件隔离。
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


async def test_hot_policy_keeps_running_turn_frozen_and_applies_to_next_turn(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    config = case.container.config_service
    before = config.get_snapshot()
    initial_policy = before.to_dict()["context"]["resource_activation"]
    assert initial_policy["default_boundary"] == "turn"
    agents = case.workspace / "AGENTS.md"
    versions = ("POLICY_TURN_AGENTS_A\n", "POLICY_TURN_AGENTS_B 🧭\n", "POLICY_TURN_CURRENT_C\n")
    agents.write_text(versions[0], encoding="utf-8")
    session = await create_session(case.client, "activation策略热更新")
    main = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main.status_code == 200, main.text
    thread = main.json()["data"]["thread_id"]
    first_call, followup_call = await case.plan(session, [
        _tool_reply("read_file", {"path": "README.md"}, "policy-frozen-read"),
        {"content": "本轮保持原策略。"},
    ])
    case.control.arm(first_call, "model.response_ready")
    accepted = await _send(case, session, "读取README后回答。")
    first_job = accepted["job_id"]
    assert isinstance(first_job, str)
    saver = case.container.checkpointer
    try:
        await asyncio.to_thread(case.control.acknowledged, first_call, "model.response_ready", 45)
        first, = saver.list_context_assemblies(session, thread)
        assert first.model_call_id == first_call
        first_bytes = canonical_json_bytes(first.to_dict())
        first_projection = saver.project_context_plan_to_history_with_diagnostics(session, thread, first.as_sealed_plan())
        first_activation = first_projection.resource_activation
        assert first_activation is not None and first_activation.activation_policy.to_dict() == initial_policy
        assert versions[0].strip() in json.dumps(case.request(first_call)["messages"], ensure_ascii=False)

        # 真实文件来源交给原配置owner解析、校验、发布；不写activation或执行状态。
        path = case.workspace / ".boxteam/workspace.jsonc"
        payload = read_jsonc_object(path)
        context_config = payload.setdefault("context", {})
        assert isinstance(context_config, dict)
        activation_config = context_config.setdefault("resource_activation", {})
        assert isinstance(activation_config, dict)
        activation_config["default_boundary"] = "model_call"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        agents.write_text(versions[1], encoding="utf-8")
        # 等真实watcher和生命周期candidate applier；直接reload会跳过原候选审批。
        async with asyncio.timeout(30):
            while True:
                status = await case.client.get("/api/v1/config/reload-status")
                assert status.status_code == 200, status.text
                published = status.json()["data"]
                if published["revision"] != before.revision or published["restart_required"] or not published["healthy"]:
                    break
                await asyncio.sleep(0.05)
        assert published["healthy"] and not published["restart_required"], published
        after = config.get_snapshot()
        expected_policy = {**initial_policy, "default_boundary": "model_call"}
        assert after.revision != before.revision
        assert published["revision"] == after.revision
        assert after.to_dict()["context"]["resource_activation"] == expected_policy
        events = await case.client.get("/api/v1/config/events")
        assert events.status_code == 200, events.text
        changed = "/context/resource_activation/default_boundary"
        applied, = [event for event in events.json()["data"]["events"]
                    if event["result"] == "applied" and changed in event["changed_paths"]]
        assert applied["activation_scope"] == "next_job"
        assert applied["changed_paths"] == applied["applied_paths"] == [changed]
        assert applied["deferred_paths"] == []
        assert canonical_json_bytes(saver.get_context_assembly(session, thread, assembly_id=first.assembly_id).to_dict()) == first_bytes
    finally:
        case.control.release(first_call, "model.response_ready")
    await _job(case, first_job)
    next_call, = await case.plan(session, [{"content": "新轮采用model_call策略与更新来源。"}])
    next_input = await _send(case, session, "按当前来源回答。")
    next_job = next_input["job_id"]
    assert isinstance(next_job, str)
    await _job(case, next_job)
    calls = (first_call, followup_call, next_call)
    captures = {call: json.loads((case.model.artifacts / f"{call}.json").read_text()) for call in calls}
    first_wire, followup_wire, next_wire = [captures[call]["request"]["messages"] for call in calls]
    assert followup_wire[:len(first_wire)] == first_wire
    assert [row["role"] for row in followup_wire[len(first_wire):]] == ["assistant", "tool"]
    assert followup_wire[-1]["tool_call_id"] == "policy-frozen-read"
    assert versions[1].strip() not in json.dumps(followup_wire, ensure_ascii=False)
    assert versions[1].strip() in json.dumps(next_wire, ensure_ascii=False)
    # 当前来源再变化也不得污染冷读的旧请求和策略。
    agents.write_text(versions[2], encoding="utf-8")
    node = saver._storage.storage.root(session, thread_id=thread)
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    audit = audit_sealed_requests(node / "index.sqlite",
        (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"), captures)
    frozen = []
    with RolloutCheckpointSaver(case.workspace / ".boxteam/sessions") as cold:
        by_call = {row.model_call_id: row for row in cold.list_context_assemblies(session, thread)}
        assert set(by_call) == set(calls)
        assert canonical_json_bytes(by_call[first_call].to_dict()) == first_bytes
        for index, call in enumerate(calls):
            snapshot = by_call[call]
            projection = cold.project_context_plan_to_history_with_diagnostics(session, thread, snapshot.as_sealed_plan())
            activation = projection.resource_activation
            assert activation is not None
            assert (activation.owner_session_id, activation.owner_thread_id) == (session, thread)
            assert activation.activation_policy.to_dict() == (initial_policy if index < 2 else expected_policy)
            bindings = [row for row in activation.bindings if row.resource_kind == "workspace_agents"]
            assert {row.facet for row in bindings} == {"chain", "raw-file"}
            assert all(row.effective_boundary == ("turn" if index < 2 else "model_call") for row in bindings)
            if index == 1:
                assert activation == first_activation
            assert versions[2].strip() not in json.dumps(snapshot.request_hash_preimage, ensure_ascii=False)
            frozen.append(activation.to_dict())
        assert by_call[first_call].turn_id == by_call[followup_call].turn_id != by_call[next_call].turn_id
    case.write_actual("activation-policy-turn-boundary", {
        "calls": calls, "reload_status": published,
        "config_event": applied,
        "config_before": before.revision, "config_after": after.revision,
        "activations": frozen, "audit": audit,
    })
