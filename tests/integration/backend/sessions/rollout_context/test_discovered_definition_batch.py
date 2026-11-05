"""真实发现、压缩和冷恢复中，按原调用及 activation 批量读取冻结定义。"""

import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.policy import ToolPolicyResolver
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogUnavailableError,
)
from app.services.infrastructure.mcp.tool_definitions import (
    tool_catalog_from_activation,
)
from app.services.infrastructure.rollout_context.checkpoint.durable import (
    source_activation,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.checkpoint.seal import preparation
from app.services.infrastructure.rollout_context.runtime.context_sources import (
    tool_definitions,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDescriptor,
    ContextSourceSnapshot,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.tool_definitions import (
    definition_source_values,
)
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

    async def create(source_count, call_count, *, damage=None, compact=True, split_calls=False):
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
        groups = [targets[index::call_count] if split_calls else targets for index in range(call_count)]
        payloads = [json.loads(await tool.ainvoke({"tool_ids": group, "max_response_bytes": 65536})) for group in groups]
        for group, payload in zip(groups, payloads, strict=True):
            assert [entry["tool_id"] for entry in payload["results"]] == list(group)
        originals = {entry["tool_id"]: entry for payload in payloads for entry in payload["results"]}
        payload = payloads[-1]
        if damage == "activation":
            payload["results"][-1]["provenance"]["activation_snapshot_id"] = "different-activation"
        elif damage == "revision":
            payload["results"][-1]["provenance"]["revision"] = "different-revision"
        elif damage == "inline":
            payload["results"][-1]["description"] = "被改写的发现正文"
        messages = [AIMessage(content="", id="batch-discovery-assistant",
            response_metadata={"model_call_id": snapshot.model_call_id},
            tool_calls=[{"name": "search_tools", "id": f"batch-discovery-{index}", "args": {"tool_ids": group}}
                        for index, group in enumerate(groups)])]
        messages.extend(ToolMessage(
            content=json.dumps(payload), id=f"batch-discovery-result-{index}", name="search_tools",
            tool_call_id=f"batch-discovery-{index}", response_metadata={"model_call_id": snapshot.model_call_id},
        ) for index, payload in enumerate(payloads))
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


@pytest.mark.parametrize("ordinary_count", [0, 8])
@pytest.mark.parametrize("compact", [False, True])
async def test_prepare_checks_definition_owner_collisions_with_one_batch_pass(
    discovered_batch, guidance_prepare, registry_db, monkeypatch, request, ordinary_count, compact,
):
    originals, _activation_id = await discovered_batch(4, 1, compact=compact)
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)

    def make_manager(owner):
        manager = ContextSourceManager(owner=ContextSourceOwnerKey(session, thread), control_state_port=owner)
        for index in range(ordinary_count):
            source_id = f"instruction:definition-batch-{index}"
            manager.register(ContextSourceDescriptor(
                source_id=source_id, source_kind="agent_instructions", name=source_id,
                description="定义恢复并存的普通来源", internal_locator=f"runtime:definition-batch-{index}",
            ), tracking_status="tracked")
            manager.observe(source_id, f"普通来源正文 {index}", revision="instruction:1")
        return manager

    manager = make_manager(saver)
    prepare = preparation.tool_definition_batch
    original_attribute = ContextSourceSnapshot.__getattribute__
    phases, active = [], []

    def observe_attribute(source, name):
        caller = sys._getframe(1).f_code
        if (active and name == "descriptor" and caller.co_filename == tool_definitions.__file__
                and caller.co_qualname.startswith("tool_definition_batch")):
            active[-1]["descriptor_reads"] += 1
        return original_attribute(source, name)

    def observe_prepare(batch, **kwargs):
        phase = {"source_count": len(batch.sources), "descriptor_reads": 0}
        phases.append(phase)
        active.append(phase)
        try:
            return prepare(batch, **kwargs)
        finally:
            active.pop()

    monkeypatch.setattr(ContextSourceSnapshot, "__getattribute__", observe_attribute)
    monkeypatch.setattr(preparation, "tool_definition_batch", observe_prepare)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for key, owner in (("first", saver), ("successor", saver), ("cold", cold)):
            source_manager = manager if owner is saver else make_manager(owner)
            wire, _ = guidance_prepare("definition-owner-" + key, context_owner=owner, context_source_manager=source_manager)
            snapshot = wire["snapshot"]
            values = [value for value, _revision in definition_source_values(snapshot)]
            assert [value.tool_id for value in values] == (sorted(originals) if compact else [])
            assert {ref.source_identity for frame in snapshot.seal_binding.prefix.state.frames for ref in frame.item_refs} >= {
                f"instruction:definition-batch-{index}" for index in range(ordinary_count)
            }
            assert cold.get_context_assembly(session, thread, assembly_id=snapshot.assembly_id).to_dict() == snapshot.to_dict()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"definition-owner-{ordinary_count}-{compact}.json").write_text(
        json.dumps(phases, indent=2), encoding="utf-8",
    )
    print(f"definition owner work ({ordinary_count=}, {compact=}): {phases}")
    assert len(phases) == 3
    assert all(phase["descriptor_reads"] == (phase["source_count"] if compact else 0) for phase in phases)


async def test_prepare_rejects_definition_registered_with_a_second_owner(
    discovered_batch, guidance_prepare, registry_db,
):
    originals, _activation_id = await discovered_batch(4, 1)
    saver, session, connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    manager = ContextSourceManager(owner=ContextSourceOwnerKey(session, thread), control_state_port=saver)
    source_id = originals["notes"]["provenance"]["resource_id"]
    manager.register(ContextSourceDescriptor(
        source_id=source_id, source_kind="agent_instructions", name="冲突来源",
        description="不得覆盖原冻结定义", internal_locator="runtime:conflicting-definition",
    ), tracking_status="tracked")
    manager.observe(source_id, "第二 owner 的正文", revision="conflicting:1")
    before = connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall()
    pending = manager.prepare_pending()
    with pytest.raises(ExtensionCatalogUnavailableError, match="活动定义不得登记第二个 CSM owner"):
        guidance_prepare("definition-owner-rejected", context_source_manager=manager)
    assert connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall() == before
    assert manager.prepare_pending() == pending


@pytest.mark.parametrize("call_count", [1, 2])
async def test_seal_preserves_discovery_provenance_in_successor_and_cold_restore(
    discovered_batch, guidance_prepare, registry_db, call_count,
):
    originals, _activation_id = await discovered_batch(4, call_count, split_calls=True)
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for key, owner in (("restored", saver), ("successor", saver), ("cold", cold)):
            wire, _ = guidance_prepare("batch-sql-" + key, context_owner=owner)
            restored = [value for value, _ in definition_source_values(wire["snapshot"])]
            assert [value.tool_id for value in restored] == sorted(originals)
            assert {value.discovered_from.tool_call_id for value in restored} == {
                f"batch-discovery-{index}" for index in range(call_count)
            }
            for value in restored:
                assert value.definition is not None
                assert value.definition.input_schema == originals[value.tool_id]["input_schema"]
                assert value.definition.provenance.model_dump() == originals[value.tool_id]["provenance"]
            assert cold.get_context_assembly(
                session, thread, assembly_id=wire["snapshot"].assembly_id,
            ).to_dict() == wire["snapshot"].to_dict()


@pytest.mark.parametrize("damage", ["missing", "unknown", "revision", "previous_revision", "control_revision", "redundant"])
async def test_seal_rejects_invalid_definition_receipts_atomically(
    discovered_batch, guidance_prepare, registry_db, monkeypatch, damage,
):
    await discovered_batch(4, 1)
    _saver, _session, connection, _accepted = registry_db
    if damage == "redundant":
        guidance_prepare("batch-receipt-already-applied")
    before = connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall()
    tables = ("context_assembly_selections", "resource_activation_assembly_bindings")
    counts = tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone() for table in tables)
    validate = source_activation._validate_definition_sources
    injected = False

    def damage_receipt(write_connection, snapshot, previous):
        nonlocal injected
        injected = True
        binding = snapshot.seal_binding
        candidates = previous.seal_binding.source_revisions if damage == "redundant" else binding.source_revisions
        target = next(source for source in candidates
                      if tool_definitions.is_tool_definition_source(source.source_id))
        if damage == "redundant":
            receipts = binding.source_revisions + (target,)
        elif damage == "missing":
            receipts = tuple(source for source in binding.source_revisions if source != target)
        else:
            changes = {
                "unknown": {"source_id": "extension-tool-definition:v1:unselected"},
                "revision": {"revision": "unselected-revision"},
                "previous_revision": {"previous_revision": "unselected-predecessor"},
                "control_revision": {"expected_control_revision": 1},
            }[damage]
            receipts = tuple(replace(source, **changes) if source == target else source
                             for source in binding.source_revisions)
        return validate(write_connection, replace(snapshot, seal_binding=replace(binding, source_revisions=receipts)), previous)

    monkeypatch.setattr(source_activation, "_validate_definition_sources", damage_receipt)
    message = "回执缺少真实 selected 来源" if damage == "unknown" else "applied 回执与封存前驱不一致"
    with pytest.raises(ItemSchemaError, match=message):
        guidance_prepare("batch-receipt-rejected:" + damage)
    assert injected
    assert connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall() == before
    assert tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone() for table in tables) == counts


@pytest.mark.parametrize("damage", ["binding", "discovery"])
async def test_seal_does_not_reuse_proofs_for_another_resource_or_tool_call(
    discovered_batch, guidance_prepare, registry_db, monkeypatch, damage,
):
    originals, activation_id = await discovered_batch(4, 2, split_calls=True)
    _saver, _session, connection, _accepted = registry_db
    before_assemblies = connection.execute("SELECT count(*) FROM context_assemblies").fetchone()
    before_bindings = connection.execute(
        "SELECT * FROM resource_activation_bindings WHERE activation_snapshot_id=? ORDER BY resource_id", (activation_id,),
    ).fetchall()
    before_calls = connection.execute("SELECT * FROM tool_calls ORDER BY tool_call_id").fetchall()
    validate = source_activation._validate_definition_sources
    injected = False

    def damage_before_validation(write_connection, snapshot, previous):
        nonlocal injected
        assert not injected
        injected = True
        # 在真实 seal 事务内注入损坏，首个资源/调用的成功证明不能授权后面的不同身份。
        if damage == "binding":
            changed = write_connection.execute(
                "UPDATE resource_activation_bindings SET availability='unavailable' "
                "WHERE activation_snapshot_id=? AND resource_id=?",
                (activation_id, originals["notes"]["provenance"]["resource_id"]),
            )
        else:
            call_id, = next(row for row in write_connection.execute("SELECT tool_call_id FROM tool_calls")
                            if row[0].endswith("batch-discovery-1"))
            changed = write_connection.execute(
                "UPDATE tool_calls SET tool_name='removed_search_tools' WHERE tool_call_id=?", (call_id,),
            )
        assert changed.rowcount == 1
        return validate(write_connection, snapshot, previous)

    monkeypatch.setattr(source_activation, "_validate_definition_sources", damage_before_validation)
    message = "冻结定义缺失或身份不唯一" if damage == "binding" else "原发现调用不属于所指冻结请求"
    with pytest.raises(ItemSchemaError, match=message):
        guidance_prepare("batch-sql-rejected")
    assert injected
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == before_assemblies
    # 新请求的 freeze 在 seal 前独立提交；本事务必须回滚对原冻结 binding 的注入。
    assert connection.execute(
        "SELECT * FROM resource_activation_bindings WHERE activation_snapshot_id=? ORDER BY resource_id", (activation_id,),
    ).fetchall() == before_bindings
    assert connection.execute("SELECT * FROM tool_calls ORDER BY tool_call_id").fetchall() == before_calls


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
    source_values = tool_definitions.definition_source_values
    phases = []

    def observe_load(owner, *args, **kwargs):
        phases[-1]["calls"].append((kwargs["tool_call_id"], kwargs["model_call_id"]))
        return load(owner, *args, **kwargs)

    def observe_catalog(frozen, read_payload, **kwargs):
        if frozen.activation_snapshot_id == activation_id:
            phases[-1]["catalogs"] += 1
        return catalog(frozen, read_payload, **kwargs)

    def observe_read(owner, frozen, binding, **kwargs):
        if frozen.activation_snapshot_id == activation_id:
            phases[-1]["bodies"].append(binding.resource_id)
        return read(owner, frozen, binding, **kwargs)

    def observe_source_values(assembly, retained=None):
        for value, revision in source_values(assembly, retained):
            phases[-1]["source_values"].append((assembly.assembly_id, value.tool_id, revision))
            yield value, revision

    monkeypatch.setattr(RolloutCheckpointSaver, "load_tool_call_resource_activation", observe_load)
    monkeypatch.setattr(preparation, "tool_catalog_from_activation", observe_catalog)
    monkeypatch.setattr(RolloutCheckpointSaver, "read_resource_activation_payload", observe_read)
    monkeypatch.setattr(tool_definitions, "definition_source_values", observe_source_values)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for key, owner in (("restored", saver), ("successor", saver), ("cold", cold)):
            phases.append({"phase": key, "calls": [], "catalogs": 0, "bodies": [], "source_values": []})
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
    # 首次压缩从实际发现结果恢复；续读及冷读各验证前驱定义一次，
    # 取得 previous revision 时不得再次解码并计算同一完整正文的摘要。
    assert [len(phase["source_values"]) for phase in phases] == [0, source_count, source_count]
    assert all(len(set(phase["source_values"])) == len(phase["source_values"]) for phase in phases)


@pytest.mark.parametrize("same_retained", [False, True])
async def test_previous_definition_revision_is_not_taken_from_a_partial_retained_set(
    discovered_batch, guidance_prepare, registry_db, monkeypatch, same_retained,
):
    await discovered_batch(4, 1)
    prepare = preparation.tool_definition_batch
    captured = []

    def observe_prepare(batch, **kwargs):
        catalogs, originals = {}, {}

        def read_catalog(tool_ids):
            catalogs[tool_ids] = kwargs["catalog_supplier"](tool_ids)
            return catalogs[tool_ids]

        def resolve(refs):
            values = kwargs["resolve_discoveries"](refs)
            originals.update(values)
            return values

        result = prepare(batch, **{**kwargs, "catalog_supplier": read_catalog, "resolve_discoveries": resolve})
        # 重放冻结值，不把只在本次准备事务内有效的 I/O 回调带出作用域。
        captured.append((batch, {**kwargs, "catalog_supplier": catalogs.__getitem__,
                                 "resolve_discoveries": lambda refs: {ref: originals[ref] for ref in refs}}))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(preparation, "tool_definition_batch", observe_prepare)
        wire, _ = guidance_prepare("batch-revision-restored")
    previous = wire["snapshot"]
    expected = {value.discovered_from.resource_id: revision
                for value, revision in definition_source_values(previous)}
    assert len(expected) == 4
    batch, options = captured[-1]
    # 重放真实 prepare 冻结的发现输入：新前驱已封存这些定义，但保留来源
    # 来自更早 assembly，或只保留同一 assembly 的空选集。
    assert options["retained_assembly"] is not previous
    options = {**options, "previous": previous}
    if same_retained:
        options.update(retained_assembly=previous, retained_source_refs=frozenset())
    result = prepare(batch, **options)
    assert {source.descriptor.source_id: source.applied_revision for source in result.sources
            if source.descriptor.source_id in expected} == expected
    # 完整前驱已有相同 revision，不能因受限来源丢失基准而补造 activation delta。
    assert not [delta for delta in result.deltas if delta.source_id in expected]


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
