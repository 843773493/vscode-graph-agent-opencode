"""真实来源同时变化时，Turn 与 model_call 边界独立冻结并保留原调用 Skill。"""

from __future__ import annotations

import asyncio
import json
from itertools import pairwise

import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.skill_catalog import (
    skill_catalog_from_activation,
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


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_mixed_sources_keep_turn_agents_and_call_frozen_skill(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = source_case
    name = "mixed-boundary"
    agents = case.workspace / "AGENTS.md"
    skill = case.workspace / ".boxteam/skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    agent_versions = ("MIXED_AGENTS_A\n", "MIXED_AGENTS_B Ω\n", "MIXED_AGENTS_C 🧭\n")
    bodies = ("MIXED_SKILL_BODY_A\n", "MIXED_SKILL_BODY_B é\n", "MIXED_SKILL_BODY_C Ω\n")

    def write_sources(index: int) -> None:
        agents.write_text(agent_versions[index], encoding="utf-8")
        skill.write_text(f"---\nname: {name}\ndescription: 同一metadata的混合边界验收\n---\n{bodies[index]}", encoding="utf-8")

    write_sources(0)
    config_path = case.workspace / ".boxteam/workspace.jsonc"
    config = read_jsonc_object(config_path)
    context_config = config.setdefault("context", {})
    assert isinstance(context_config, dict)
    activation_config = context_config.setdefault("resource_activation", {})
    assert isinstance(activation_config, dict)
    activation_config["overrides"] = {"skill": "model_call"}
    before_revision = case.container.config_service.get_snapshot().revision
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 保留生产watcher和candidate applier，不直接发布配置或执行冻结状态。
    async with asyncio.timeout(30):
        while True:
            response = await case.client.get("/api/v1/config/reload-status")
            assert response.status_code == 200, response.text
            status = response.json()["data"]
            if status["revision"] != before_revision or status["restart_required"] or not status["healthy"]:
                break
            await asyncio.sleep(0.05)
    assert status["healthy"] and not status["restart_required"], status
    expected_policy = case.container.config_service.get_snapshot().to_dict()["context"]["resource_activation"]
    assert expected_policy["default_boundary"] == "turn"
    assert expected_policy["overrides"]["skill"] == "model_call"

    # 仅记录真实Registry发布后的结果；不替代loader、reconciler或冻结操作。
    publications: dict[str, ResourceSnapshot] = {}
    publish = ResourceRegistry.publish

    def observe_publication(registry: ResourceRegistry, snapshot: ResourceSnapshot) -> ResourceSnapshot:
        result = publish(registry, snapshot)
        publications[result.resource_id] = result
        return result

    monkeypatch.setattr(ResourceRegistry, "publish", observe_publication)
    session = await create_session(case.client, "同请求混合来源边界")
    main = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main.status_code == 200, main.text
    thread = main.json()["data"]["thread_id"]
    first, second, third = await case.plan(session, [
        _tool_reply("skill_load", {"name": name}, "mixed-load-a"),
        _tool_reply("skill_load", {"name": name}, "mixed-load-b"),
        {"content": "同轮AGENTS保持A，Skill显式加载到B。"},
    ])
    for call in (first, second):
        case.control.arm(call, "model.response_ready")
    try:
        accepted = await _send(case, session, "显式加载两次Skill并核对来源。")
        job_id = accepted["job_id"]
        assert isinstance(job_id, str)
        for index, call in enumerate((first, second), start=1):
            await asyncio.to_thread(case.control.acknowledged, call, "model.response_ready", 45)
            # 发起工具调用的请求已封存；两个实际文件同时更新，等待语义发布后才放行工具。
            write_sources(index)
            async with asyncio.timeout(15):
                while True:
                    skill_ready = any(row.resource_kind == "skill" and row.facet == "activation"
                                      and row.payload == {"body": bodies[index]} for row in publications.values())
                    agents_ready = any(row.resource_kind == "workspace_agents" and row.facet == "raw-file"
                                       and agent_versions[index].strip() in json.dumps(row.payload, ensure_ascii=False)
                                       for row in publications.values())
                    if skill_ready and agents_ready:
                        break
                    await asyncio.sleep(0.05)
            case.control.release(call, "model.response_ready")
    finally:
        for call in (first, second):
            case.control.release(call, "model.response_ready")
    await _job(case, job_id)

    fourth, fifth = await case.plan(session, [
        _tool_reply("skill_load", {"name": name}, "mixed-load-c"),
        {"content": "新轮采用C来源。"},
    ])
    next_input = await _send(case, session, "新轮加载最新Skill。")
    next_job_id = next_input["job_id"]
    assert isinstance(next_job_id, str)
    await _job(case, next_job_id)
    calls = (first, second, third, fourth, fifth)
    captures = {call: json.loads((case.model.artifacts / f"{call}.json").read_text()) for call in calls}
    for call in calls:
        assert captures[call]["request"] == case.request(call)
    wires = [captures[call]["request"]["messages"] for call in calls]
    receipts = []
    for index, (call_id, expected_body) in enumerate(zip(
        ("mixed-load-a", "mixed-load-b", "mixed-load-c"), bodies, strict=True,
    )):
        wire = wires[(1, 2, 4)[index]]
        receipt, = [json.loads(row["content"]) for row in wire
                    if row["role"] == "tool" and row["tool_call_id"] == call_id]
        assert receipt["status"] == "loaded" and receipt["error"] is None and not receipt["tracked"], receipt
        assert expected_body.strip() in json.dumps(wire, ensure_ascii=False)
        assert expected_body.strip() not in json.dumps(receipt, ensure_ascii=False)
        assert receipt["display_uri"].startswith("boxteam://workspace/")
        receipts.append(receipt)
    assert len({row["revision"] for row in receipts}) == 3
    for index, wire in enumerate(wires):
        encoded = json.dumps(wire, ensure_ascii=False)
        if index < 3:
            assert agent_versions[0].strip() in encoded
            assert all(version.strip() not in encoded for version in agent_versions[1:])
        else:
            assert agent_versions[2].strip() in encoded
        if index < 4:
            assert bodies[2].strip() not in encoded
    for previous, current in pairwise(wires[:3]):
        assert current[:len(previous)] == previous
        assert [row["role"] for row in current[len(previous):len(previous) + 2]] == ["assistant", "tool"]

    saver = case.container.checkpointer
    originals = {row.model_call_id: canonical_json_bytes(row.to_dict())
                 for row in saver.list_context_assemblies(session, thread)}
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    audit = audit_sealed_requests(saver._storage.storage.root(session, thread_id=thread) / "index.sqlite",
        (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"), captures)
    # 当前文件已不可读；冷恢复只能使用原owner封存正文与lineage，不能猜当前来源。
    agents.unlink()
    skill.unlink()
    frozen = []
    receipt_by_call = {first: receipts[0], second: receipts[1], fourth: receipts[2]}
    with RolloutCheckpointSaver(case.workspace / ".boxteam/sessions") as cold:
        assemblies = {row.model_call_id: row for row in cold.list_context_assemblies(session, thread)}
        assert set(assemblies) == set(calls)
        for index, call in enumerate(calls):
            assembly = assemblies[call]
            assert canonical_json_bytes(assembly.to_dict()) == originals[call]
            activation = cold.load_sealed_resource_activation(session, thread_id=thread,
                assembly_id=assembly.assembly_id, plan_hash=assembly.plan_hash, request_hash=assembly.request_hash)
            assert (activation.owner_session_id, activation.owner_thread_id) == (session, thread)
            assert activation.activation_policy.to_dict() == expected_policy
            catalog = skill_catalog_from_activation(activation, lambda snapshot, binding:
                cold.read_resource_activation_payload(snapshot, binding, storage_owner=(session, thread)))
            assert catalog is not None
            assert catalog.entries[name].body == bodies[min(index, 2)]
            if call in receipt_by_call:
                assert catalog.entries[name].activation_revision == receipt_by_call[call]["revision"]
            agent_refs = [row for row in activation.bindings if row.resource_kind == "workspace_agents"]
            assert {row.facet for row in agent_refs} == {"chain", "raw-file"}
            assert all(row.effective_boundary == "turn" for row in agent_refs)
            raw_agents, = [row for row in agent_refs if row.facet == "raw-file"]
            agent_body = cold.read_resource_activation_payload(activation, raw_agents, storage_owner=(session, thread))
            assert agent_versions[0 if index < 3 else 2].strip() in json.dumps(agent_body, ensure_ascii=False)
            skill_refs = [row for row in activation.bindings if row.resource_kind == "skill"
                          and row.display_uri == receipts[0]["display_uri"]]
            assert {row.facet for row in skill_refs} == {"metadata", "activation"}
            assert all(row.effective_boundary == "model_call" for row in skill_refs)
            frozen.append(activation)
            projection = cold.read_context_assembly_history(session, thread, assembly.assembly_id)
            assert projection.resource_activation == activation
        # 同一raw来源的metadata语义未变，但正文和lineage确实变化。
        metadata = [next(row for row in activation.bindings if row.resource_kind == "skill"
                         and row.facet == "metadata" and row.display_uri == receipts[0]["display_uri"])
                    for activation in frozen[:3]]
        assert len({row.revision for row in metadata}) == 1
        assert len({row.source_lineage_digest for row in metadata}) == 3
        for current in frozen[1:3]:
            assert [row for row in current.bindings if row.resource_kind == "workspace_agents"] == [
                row for row in frozen[0].bindings if row.resource_kind == "workspace_agents"]
        assert assemblies[first].turn_id == assemblies[third].turn_id != assemblies[fourth].turn_id
    case.write_actual("mixed-activation-sources", {
        "calls": calls, "receipts": receipts, "audit": audit,
        "activations": [row.to_dict() for row in frozen],
    })
