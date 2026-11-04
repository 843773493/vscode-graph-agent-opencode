"""真实发现、压缩和冷恢复中，按原调用及 activation 批量读取冻结定义。"""

import json
from collections import Counter
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.policy import ToolPolicyResolver
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.mcp.extension_catalog import ExtensionCatalogUnavailableError
from app.services.infrastructure.mcp.tool_definitions import tool_catalog_from_activation
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.checkpoint.seal import preparation
from app.services.infrastructure.rollout_context.runtime.context_sources.tool_definitions import definition_source_values
from app.services.infrastructure.rollout_context.runtime.context_sources import tool_definitions
from tests.harness.python.run_context import TestRunContext
from tests.unit.services.infrastructure.mcp import test_tool_search as search

registry_db = search.registry_db
catalog_workspace = search.catalog_workspace
catalog_runtime = search.catalog_runtime
discovery_store = search.discovery_store
discovery_tool_factory = search.discovery_tool_factory
guidance_prepare = search.guidance_prepare


@pytest.fixture
def discovered_batch(guidance_prepare, registry_db, discovery_tool_factory):
    saver, session, _connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    admission = saver.execution_admission(session, thread_id=thread, execution_id=accepted["initial_execution_id"])

    async def create(source_count, call_count, *, damage=None, compact=True):
        wire, activation = guidance_prepare("batch-discovery-start")
        snapshot = wire["snapshot"]
        catalog = tool_catalog_from_activation(activation, lambda frozen, binding: saver.read_resource_activation_payload(
            frozen, binding, storage_owner=(session, thread),
        ))
        resolver = ToolPolicyResolver(policy_defaults={"model_visible": False}, policy_rules={}, restrictions={})
        tool = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=lambda frozen: {
            definition.metadata.tool_id: resolver.resolve(definition.metadata) for definition in frozen.definitions
        })
        targets = ("ci_logs", "ci_runs", "notes", "mcp__mini__echo")[:source_count]
        payload = json.loads(await tool.ainvoke({"tool_ids": targets, "max_response_bytes": 65536}))
        assert [entry["tool_id"] for entry in payload["results"]] == list(targets)
        originals = {entry["tool_id"]: entry for entry in payload["results"]}
        if damage == "activation":
            payload["results"][-1]["provenance"]["activation_snapshot_id"] = "different-activation"
        elif damage == "revision":
            payload["results"][-1]["provenance"]["revision"] = "different-revision"
        elif damage == "inline":
            payload["results"][-1]["description"] = "被改写的发现正文"
        messages = [AIMessage(content="", id="batch-discovery-assistant",
            response_metadata={"model_call_id": snapshot.model_call_id},
            tool_calls=[{"name": "search_tools", "id": f"batch-discovery-{index}", "args": {"tool_ids": targets}}
                        for index in range(call_count)])]
        messages.extend(ToolMessage(
            content=json.dumps(payload), id=f"batch-discovery-result-{index}", name="search_tools",
            tool_call_id=f"batch-discovery-{index}", response_metadata={"model_call_id": snapshot.model_call_id},
        ) for index in range(call_count))
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "batch-discovery-checkpoint"
        checkpoint["channel_values"] = {"messages": messages}
        if compact:
            checkpoint["channel_values"]["_summarization_event"] = {
                "cutoff_index": len(messages), "cutoff_message_id": messages[-1].id,
                "summary_message": HumanMessage(content="已发现批次工具。", id="batch-discovery-summary"),
            }
        checkpoint["channel_versions"] = {name: "1" for name in checkpoint["channel_values"]}
        checkpoint["updated_channels"] = list(checkpoint["channel_values"])
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            saver.register_model_call(session, thread_id=thread, execution_id=admission.execution_id,
                model_call_id=snapshot.model_call_id, assembly_id=snapshot.assembly_id,
                attempt=1, provider="fixture-model", dispatch_state="dispatched")
            saver.put(build_checkpoint_config(thread), checkpoint,
                {"source": "compaction" if compact else "loop", "step": 1}, checkpoint["channel_versions"])
        return originals, activation.activation_snapshot_id

    return create


@pytest.mark.parametrize("source_count", [1, 4])
@pytest.mark.parametrize("call_count", [1, 2])
async def test_prepare_reads_discovered_catalog_once_per_activation(
    discovered_batch, guidance_prepare, registry_db, monkeypatch, request, source_count, call_count,
):
    originals, activation_id = await discovered_batch(source_count, call_count)
    saver, session, connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    load = RolloutCheckpointSaver.load_tool_call_resource_activation
    catalog = preparation.tool_catalog_from_activation
    read = RolloutCheckpointSaver.read_resource_activation_payload
    phases = []

    def observe_load(owner, *args, **kwargs):
        phases[-1]["calls"].append((kwargs["tool_call_id"], kwargs["model_call_id"]))
        return load(owner, *args, **kwargs)

    def observe_catalog(frozen, read_payload):
        if frozen.activation_snapshot_id == activation_id:
            phases[-1]["catalogs"] += 1
        return catalog(frozen, read_payload)

    def observe_read(owner, frozen, binding, **kwargs):
        if frozen.activation_snapshot_id == activation_id:
            phases[-1]["bodies"].append(binding.resource_id)
        return read(owner, frozen, binding, **kwargs)

    monkeypatch.setattr(RolloutCheckpointSaver, "load_tool_call_resource_activation", observe_load)
    monkeypatch.setattr(preparation, "tool_catalog_from_activation", observe_catalog)
    monkeypatch.setattr(RolloutCheckpointSaver, "read_resource_activation_payload", observe_read)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for key, owner in (("restored", saver), ("successor", saver), ("cold", cold)):
            phases.append({"phase": key, "calls": [], "catalogs": 0, "bodies": []})
            wire, _ = guidance_prepare("batch-discovery-" + key, context_owner=owner)
            snapshot = wire["snapshot"]
            restored = [value for value, _revision in definition_source_values(snapshot)]
            assert [value.tool_id for value in restored] == sorted(originals)
            for value in restored:
                assert value.status == "available" and value.definition is not None
                assert value.definition.provenance.model_dump() == originals[value.tool_id]["provenance"]
                assert value.definition.input_schema == originals[value.tool_id]["input_schema"]
                assert value.discovered_from.tool_call_id == f"batch-discovery-{call_count - 1}"
            assert canonical_json_bytes(cold.get_context_assembly(
                session, thread, assembly_id=snapshot.assembly_id,
            ).to_dict()) == canonical_json_bytes(snapshot.to_dict())
    assert connection.execute("SELECT count(*) FROM context_source_control_states WHERE source_id LIKE 'extension-tool-definition:%'").fetchone() == (0,)
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"discovered-batch-{source_count}-{call_count}.json").write_text(
        json.dumps(phases, indent=2), encoding="utf-8",
    )
    print(f"discovered batch work ({source_count} tools, {call_count} calls): {phases}")
    # 原调用逐个验证归属，同一不可变 activation 的全目录正文只在本次 prepare 读一次。
    assert [len(phase["calls"]) for phase in phases] == [call_count, 1, 1]
    assert all(max(Counter(phase["calls"]).values()) == 1 for phase in phases)
    assert all(phase["catalogs"] == 1 for phase in phases)
    assert all(max(Counter(phase["bodies"]).values()) == 1 for phase in phases)


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("call_count", [1, 2])
async def test_prepare_decodes_each_discovery_result_once(
    discovered_batch, guidance_prepare, registry_db, monkeypatch, request, compact, call_count,
):
    originals, _activation_id = await discovered_batch(4, call_count, compact=compact)
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    project = tool_definitions.project_tool_result
    phases = []

    def observe_project(item):
        phases[-1]["results"].append(item.item_id)
        return project(item)

    monkeypatch.setattr(tool_definitions, "project_tool_result", observe_project)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for key, owner in (("successor", saver), ("cold", cold)):
            phases.append({"phase": key, "results": []})
            wire, _ = guidance_prepare("batch-decode-" + key, context_owner=owner)
            values = [value for value, _revision in definition_source_values(wire["snapshot"])]
            # 当前完整发现结果保持原引用；压缩后才恢复完整来源，不能改变注入时机。
            assert [value.tool_id for value in values] == (sorted(originals) if compact else [])
            for value in values:
                assert value.definition is not None
                assert value.definition.provenance.model_dump() == originals[value.tool_id]["provenance"]
            assert cold.get_context_assembly(
                session, thread, assembly_id=wire["snapshot"].assembly_id,
            ).to_dict() == wire["snapshot"].to_dict()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"discovery-decode-{compact}-{call_count}.json").write_text(
        json.dumps(phases, indent=2), encoding="utf-8",
    )
    print(f"discovery decode work ({compact=}, {call_count=}): {phases}")
    # 同次 prepare 不重复投影、解析；普通续读及冷读仍重新验证当前结果。
    # 压缩后的冷读继承封存来源，不重新读取已离开当前选集的原始结果。
    for phase in phases:
        expected_count = 0 if compact and phase["phase"] == "cold" else call_count
        assert len(set(phase["results"])) == expected_count
        assert len(phase["results"]) == expected_count


@pytest.mark.parametrize("damage, message", [
    ("activation", "发现结果与原工具调用 activation 不一致"),
    ("revision", "发现结果引用的原冻结定义缺失或身份冲突"),
    ("inline", "发现结果正文与原冻结定义不一致"),
])
async def test_batch_reuse_rejects_mismatched_discovery_without_sealing(
    discovered_batch, guidance_prepare, registry_db, damage, message,
):
    await discovered_batch(4, 2, damage=damage)
    _saver, _session, connection, _accepted = registry_db
    before = connection.execute("SELECT count(*) FROM context_assemblies").fetchone()
    with pytest.raises(ExtensionCatalogUnavailableError, match=message):
        guidance_prepare("batch-discovery-rejected")
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == before
