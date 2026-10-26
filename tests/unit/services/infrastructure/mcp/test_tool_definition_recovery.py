"""发现后的 checkpoint 边界、策略与完整定义恢复；使用真实冻结目录。"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.policy import ToolPolicyResolver
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.runtime.context_sources.tool_definitions import (
    definition_source_values,
)
from tests.unit.services.infrastructure.mcp import test_tool_search as search

registry_db = search.registry_db
catalog_workspace = search.catalog_workspace
catalog_runtime = search.catalog_runtime
discovery_store = search.discovery_store
discovery_tool_factory = search.discovery_tool_factory
guidance_prepare = search.guidance_prepare


@pytest.fixture
def discovery_history(guidance_prepare, registry_db, discovery_tool_factory):
    saver, session, connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    admission = saver.execution_admission(session, thread_id=thread, execution_id=accepted["initial_execution_id"])

    @contextmanager
    def scope():
        with session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread), execution_admission_scope(admission, langgraph_thread_id=thread):
            yield

    async def create(*, target="ci_logs", compact=False, boundary="model_call"):
        from app.services.infrastructure.mcp.tool_definitions import (
            tool_catalog_from_activation,
        )
        wire, activation = guidance_prepare("recovery-start", boundary=boundary)
        snapshot = wire["snapshot"]
        catalog = tool_catalog_from_activation(activation, lambda frozen, binding: saver.read_resource_activation_payload(
            frozen, binding, storage_owner=(session, thread)), model_call_id=snapshot.model_call_id)
        resolver = ToolPolicyResolver(policy_defaults={"model_visible": False}, policy_rules={}, restrictions={})
        tool = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=lambda frozen: {
            definition.metadata.tool_id: resolver.resolve(definition.metadata) for definition in frozen.definitions})
        raw = await tool.ainvoke({"tool_ids": [target], "max_response_bytes": 4096})
        messages = [AIMessage(content="", id="discovery-assistant", response_metadata={"model_call_id": snapshot.model_call_id},
                              tool_calls=[{"name": "search_tools", "id": "discovery-call", "args": {"tool_ids": [target]}}]),
                    ToolMessage(content=raw, id="discovery-result", name="search_tools", tool_call_id="discovery-call",
                                response_metadata={"model_call_id": snapshot.model_call_id})]
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "recovery-checkpoint"
        checkpoint["channel_values"] = {"messages": messages}
        if compact:
            checkpoint["channel_values"]["_summarization_event"] = {
                "cutoff_index": 2, "cutoff_message_id": messages[-1].id,
                "summary_message": HumanMessage(content="已发现所需工具。", id="recovery-summary"),
            }
        checkpoint["channel_versions"] = {name: "1" for name in checkpoint["channel_values"]}
        checkpoint["updated_channels"] = list(checkpoint["channel_values"])
        with scope():
            saver.register_model_call(session, thread_id=thread, execution_id=admission.execution_id,
                                      model_call_id=snapshot.model_call_id, assembly_id=snapshot.assembly_id,
                                      attempt=1, provider="fixture-model", dispatch_state="dispatched")
            saver.put(build_checkpoint_config(thread), checkpoint, {"source": "compaction" if compact else "loop", "step": 1}, checkpoint["channel_versions"])
        return json.loads(raw)["results"][0], scope, connection

    return create


@pytest.mark.parametrize("target", ["ci_logs", "huge"])
async def test_immediate_compaction_restores_definition_not_present_in_previous_assembly(
    discovery_history, guidance_prepare, target,
):
    entry, _scope, connection = await discovery_history(target=target, compact=True)
    assert entry["schema_state"] == ("needs_read" if target == "huge" else "inline_complete")
    wire, _activation = guidance_prepare("recovery-after-immediate-compaction")
    restored, = [value for value, _ in definition_source_values(wire["snapshot"])]
    assert restored.tool_id == target and restored.status == "available"
    assert restored.definition is not None
    assert restored.definition.provenance.model_dump() == entry["provenance"]
    if target == "huge":
        definitions = restored.definition.input_schema["$defs"]
        assert isinstance(definitions, dict)
        run = definitions["run"]
        assert isinstance(run, dict)
        assert run["description"] == "完整大定义" * 6000
    else:
        assert restored.definition.input_schema == entry["input_schema"]
    assert connection.execute("SELECT count(*) FROM context_source_control_states WHERE source_id LIKE 'extension-tool-definition:%'").fetchone() == (0,)
    successor, _ = guidance_prepare("recovery-stable-successor")
    assert tuple(definition_source_values(successor["snapshot"])) == tuple(definition_source_values(wire["snapshot"]))


async def test_current_complete_result_does_not_inject_duplicate_definition(discovery_history, guidance_prepare):
    await discovery_history()
    wire, _ = guidance_prepare("recovery-inline-successor")
    assert not tuple(definition_source_values(wire["snapshot"]))


@pytest.mark.parametrize("restriction", ["model_hidden", "execution_disabled"])
async def test_compacted_definition_obeys_current_hard_policy(discovery_history, guidance_prepare, restriction):
    await discovery_history(compact=True)
    resolver = ToolPolicyResolver(policy_defaults={"model_visible": False}, policy_rules={}, restrictions={restriction: ["ci_logs"]})
    wire, _ = guidance_prepare("recovery-restricted", policies_for=lambda metadata: {entry.tool_id: resolver.resolve(entry) for entry in metadata})
    values = tuple(definition_source_values(wire["snapshot"]))
    if restriction == "model_hidden":
        assert not values
    else:
        value, = [value for value, _ in values]
        assert value.status == "unavailable" and value.reason == "tool-disabled" and value.definition is None


async def test_needs_read_stays_a_reference_until_restore(discovery_history, guidance_prepare):
    entry, _scope, _connection = await discovery_history(target="huge")
    assert entry["schema_state"] == "needs_read"
    wire, _ = guidance_prepare("recovery-reference-successor")
    assert not tuple(definition_source_values(wire["snapshot"]))


async def test_restore_after_restart_keeps_original_frozen_provenance(discovery_history, guidance_prepare, registry_db):
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )
    entry, _scope, _connection = await discovery_history(compact=True)
    saver, _session, _connection, _accepted = registry_db
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        wire, _ = guidance_prepare("recovery-cold", context_owner=cold)
        restored, = [value for value, _ in definition_source_values(wire["snapshot"])]
        assert restored.definition is not None
        assert restored.definition.provenance.model_dump() == entry["provenance"]
        assert restored.definition.input_schema == entry["input_schema"]


async def test_catalog_change_supplies_full_replacement_and_removal(
    discovery_history, guidance_prepare, catalog_runtime,
):
    from tests.support.mcp.session_doubles import drain_pending_relists
    entry, _scope, _connection = await discovery_history(target="mcp__mini__echo")
    _owner, factory, _registry, _lifetime = catalog_runtime
    factory.session.minimum = 7
    factory.changed()
    await drain_pending_relists(_owner)
    wire, _ = guidance_prepare("recovery-definition-replacement")
    updated, = [value for value, _ in definition_source_values(wire["snapshot"])]
    assert updated.definition is not None
    properties = updated.definition.input_schema["properties"]
    assert isinstance(properties, dict)
    text = properties["text"]
    assert isinstance(text, dict)
    assert text["minLength"] == 7
    assert updated.definition.provenance.revision != entry["provenance"]["revision"]
    assert updated.discovered_from.revision == entry["provenance"]["revision"]
    factory.session.empty = True
    factory.changed()
    await drain_pending_relists(_owner)
    removed, _ = guidance_prepare("recovery-definition-removal")
    values = [value for value, _ in definition_source_values(removed["snapshot"])]
    assert values[-1].status == "unavailable" and values[-1].reason == "tool-removed"
    assert values[-1].definition is None


async def test_missing_original_body_does_not_fall_back_to_current_catalog(
    discovery_history, guidance_prepare, registry_db, monkeypatch,
):
    from app.services.infrastructure.mcp.extension_catalog import (
        ExtensionCatalogUnavailableError,
    )
    entry, _scope, connection = await discovery_history(compact=True)
    saver, _session, _connection, _accepted = registry_db
    original_read = saver.read_resource_activation_payload

    def missing(snapshot, binding, **options):
        if (snapshot.activation_snapshot_id, binding.resource_id) == (
            entry["provenance"]["activation_snapshot_id"], entry["provenance"]["resource_id"],
        ):
            raise ExtensionCatalogUnavailableError("测试：原冻结定义正文缺失")
        return original_read(snapshot, binding, **options)

    monkeypatch.setattr(saver, "read_resource_activation_payload", missing)
    before = connection.execute("SELECT count(*) FROM context_assemblies").fetchone()
    with pytest.raises(ExtensionCatalogUnavailableError, match="原冻结定义正文缺失"):
        guidance_prepare("recovery-missing-original")
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == before


async def test_rewind_before_discovery_does_not_restore_later_definition(
    discovery_history, guidance_prepare, registry_db, catalog_runtime,
):
    from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
        acheckpoint_messages,
    )
    from tests.support.mcp.session_doubles import drain_pending_relists
    _entry, scope, _connection = await discovery_history(target="mcp__mini__echo")
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    owner, factory, _registry, _lifetime = catalog_runtime
    factory.session.minimum = 4
    factory.changed()
    await drain_pending_relists(owner)
    updated, _ = guidance_prepare("recovery-before-rewind")
    assert tuple(definition_source_values(updated["snapshot"]))
    with scope():
        loaded = await saver.aget_tuple(build_checkpoint_config(thread))
        messages = await acheckpoint_messages(saver, loaded)
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "recovery-checkpoint-with-definition"
        checkpoint["channel_values"] = {"messages": messages}
        checkpoint["channel_versions"] = {"messages": "2"}
        checkpoint["updated_channels"] = ["messages"]
        config = saver.put(loaded.config, checkpoint, {"source": "loop", "step": 2}, {"messages": "2"})
        call = next(item for item in saver._storage.storage.read_items(session, thread)
                    if item.semantic_kind == "tool_call")
        assert call.projection_identity is not None
        saver.rewind(config, checkpoint_id=checkpoint["id"], source_anchor=call.projection_identity.projection_message_id, anchor_mode="before")
    rewound, _ = guidance_prepare("recovery-after-rewind")
    assert not tuple(definition_source_values(rewound["snapshot"]))


async def test_turn_boundary_keeps_original_definition_after_live_catalog_change(
    discovery_history, guidance_prepare, catalog_runtime,
):
    from tests.support.mcp.session_doubles import drain_pending_relists
    entry, _scope, _connection = await discovery_history(target="mcp__mini__echo", boundary="turn")
    owner, factory, _registry, _lifetime = catalog_runtime
    factory.session.minimum = 9
    factory.changed()
    await drain_pending_relists(owner)
    wire, frozen = guidance_prepare("recovery-same-turn", boundary="turn")
    assert frozen.activation_snapshot_id == entry["provenance"]["activation_snapshot_id"]
    assert not tuple(definition_source_values(wire["snapshot"]))


def test_prepare_rejects_canonical_advance_between_source_read_and_composition(
    guidance_prepare, registry_db, monkeypatch,
):
    saver, session, connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    register = saver.register_context_contribution
    advanced = False

    def register_and_accept(*args, **kwargs):
        nonlocal advanced
        result = register(*args, **kwargs)
        if not advanced:
            advanced = True
            saver.accept_turn(
                session, thread_id=thread, accepted_ingress_id="recovery-racing-ingress",
                acceptance_idempotency_key="recovery-racing-acceptance", job_id="recovery-racing-job",
                admission_idempotency_key="recovery-racing-admission", payload="来源登记期间的新输入", payload_kind="text",
            )
        return result

    monkeypatch.setattr(saver, "register_context_contribution", register_and_accept)
    before = connection.execute("SELECT count(*) FROM context_assemblies").fetchone()
    with pytest.raises(RuntimeError, match="context-prepare-state-conflict"):
        guidance_prepare("recovery-racing-request")
    assert advanced
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == before
    assert connection.execute("SELECT count(*) FROM turn_records").fetchone() == (2,)
