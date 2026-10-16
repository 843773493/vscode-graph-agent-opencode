"""真实 Saver prepare → seal → CSM 确认 → Provider request 的纵向验证。"""

from __future__ import annotations

import json
import sqlite3

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.model_request_context import SummaryCandidateScope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.owner import context_owner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.provider import native_request
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
)
from app.services.mapping.itemized.canonical_langchain import (
    project_summary_source,
    summary_source_groups,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - fixture 根据当前测试文件隔离产物
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)

@pytest.fixture
def production_context(frozen_provider_activation, frozen_provider_wire, registry_db):
    saver, session, connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session, thread), control_state_port=saver,
    )
    manager.register(ContextSourceDescriptor(
        source_id="instruction:R01", source_kind="agent_instructions", name="Agent 基础说明",
        description="Agent 基础说明", internal_locator="instruction-producer://R01",
        root_placement="root_eligible",
    ), tracking_status="tracked")
    manager.observe("instruction:R01", "基础规则第一版", revision="instructions:1")
    selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=accepted["initial_execution_id"],
    )

    def prepare(
        key, *, supplier=None, target_format="chat_completions",
        request_messages=(), summary_candidate=None,
    ):
        with (
            session_thread_owner_scope(
                MutationIntentOwner(session, thread), langgraph_thread_id=thread,
            ),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            return saver.prepare_context_for_provider(
                session, thread, turn_id=accepted["turn_id"],
                execution_id=accepted["initial_execution_id"],
                plan_creation_idempotency_key="create:" + key,
                seal_idempotency_key="seal:" + key,
                request_messages=request_messages, summary_candidate=summary_candidate,
                context_source_manager=manager,
                tool_set_supplier=supplier or (lambda: selected),
                **frozen_provider_wire("production-provider", target_format),
                **frozen_provider_activation(saver, plan_creation_idempotency_key='create:' + key),
            )

    return saver, session, thread, connection, manager, prepare


def test_prepare_and_public_composition_read_predecessor_once_per_snapshot(
    production_context, monkeypatch,
):
    saver, session, thread, _connection, manager, prepare = production_context
    assemblies = saver._storage.assemblies
    original_latest = assemblies.latest_context_assembly
    original_transaction_latest = assemblies._latest_context_assembly_connection
    reads = []
    seal_predecessors = []

    def read_latest(snapshot):
        previous = original_latest(snapshot)
        reads.append((snapshot, previous))
        return previous

    def read_transaction_latest(connection, *args, **kwargs):
        if kwargs.get("excluding_assembly_id") is not None:
            assert connection.in_transaction
            seal_predecessors.append(kwargs["excluding_assembly_id"])
        return original_transaction_latest(connection, *args, **kwargs)

    monkeypatch.setattr(assemblies, "latest_context_assembly", read_latest)
    monkeypatch.setattr(assemblies, "_latest_context_assembly_connection", read_transaction_latest)
    first = prepare("snapshot-first")
    manager.observe("instruction:R01", "基础规则第二版", revision="instructions:2")
    second = prepare("snapshot-second")
    assert len(reads) == len({id(snapshot) for snapshot, _ in reads}) == 4
    assert [None if previous is None else previous.assembly_id for _, previous in reads] == [
        None, None, first["assembly_id"], first["assembly_id"],
    ]
    assert seal_predecessors == [first["assembly_id"], second["assembly_id"]]
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    restored = cold.get_context_assembly(session, thread, assembly_id=second["assembly_id"])
    messages, tools, losses = cold.project_context_plan_to_provider(
        session, thread, restored.as_sealed_plan(), target_format="chat_completions",
        include_runtime_notices=True, include_summaries=True,
    )
    assert (messages, tools, losses) == (second["messages"], second["tools"], ())
    with saver._context_reader.open_snapshot(session, thread, "") as snapshot:
        plan = saver.compose_committed_context_plan(
            session, thread, plan_id="public-snapshot",
            tool_set=ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
            read_snapshot=snapshot,
        )
        assert reads[-1][0] is snapshot
        assert reads[-1][1].assembly_id == second["assembly_id"]
    assert len(reads) == 5
    assert (plan.session_id, plan.thread_id) == (session, thread)


def test_cold_assembly_recomputes_one_plan_hash_per_validation(
    production_context, monkeypatch,
):
    saver, session, thread, _connection, _manager, prepare = production_context
    result = prepare("cold-hash-recomputation")
    original_validate = ContextAssemblySnapshot.validate_hashes
    original_plan_hash = ContextRequestPlan.plan_hash
    plans, validated = [], []

    def recompute(plan):
        plans.append(plan)
        return original_plan_hash(plan)

    def validate(assembly):
        before = len(plans)
        original_validate(assembly)
        assert len(plans) - before == 1
        assert plans[before].assembly_id == assembly.assembly_id
        validated.append(assembly.assembly_id)

    monkeypatch.setattr(ContextRequestPlan, "plan_hash", recompute)
    monkeypatch.setattr(ContextAssemblySnapshot, "validate_hashes", validate)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        first = cold.get_context_assembly(session, thread, assembly_id=result["assembly_id"])
        first_validations = len(validated)
        assert first_validations > 0
        second = cold.get_context_assembly(session, thread, assembly_id=result["assembly_id"])
        assert len(validated) > first_validations
        assert second is not first
        assert (second.plan_hash, second.request_hash) == (first.plan_hash, first.request_hash)
        assert cold.project_context_plan_to_provider(
            session, thread, second.as_sealed_plan(), target_format="chat_completions",
            include_runtime_notices=True, include_summaries=True,
        ) == (result["messages"], result["tools"], result["losses"])
    assert set(validated) == {result["assembly_id"]}


def test_new_prepare_validates_once_before_ack_and_cold_reads_after_ack(
    production_context, monkeypatch,
):
    saver, _session, _thread, _connection, manager, prepare = production_context
    validated, reads = [], []
    original_validate = ContextAssemblySnapshot.validate_hashes
    original_read = saver._storage.assemblies._read_context_assembly_connection
    original_ack = manager.acknowledge_sealed_pending

    def validate(snapshot):
        original_validate(snapshot)
        validated.append(snapshot.assembly_id)

    def read(*args, **kwargs):
        reads.append(kwargs["assembly_id"])
        return original_read(*args, **kwargs)

    def acknowledge(*args, **kwargs):
        assert len(validated) == 1
        assert reads == []
        return original_ack(*args, **kwargs)

    monkeypatch.setattr(ContextAssemblySnapshot, "validate_hashes", validate)
    monkeypatch.setattr(saver._storage.assemblies, "_read_context_assembly_connection", read)
    monkeypatch.setattr(manager, "acknowledge_sealed_pending", acknowledge)
    result = prepare("one-seal-admission")
    assert validated == [result["assembly_id"], result["assembly_id"]]
    assert reads == [result["assembly_id"]]


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_final_projection_reads_fresh_snapshot_after_ack_and_maps_after_close(
    production_context, monkeypatch, target_format,
):
    saver, session, thread, _connection, manager, prepare = production_context
    manager.observe("instruction:R01", "基础规则第一版", revision=sha256_jcs("基础规则第一版"))
    storage, assemblies = saver._storage.storage, saver._storage.assemblies
    original_open = storage.open_read_snapshot
    original_ack = manager.acknowledge_sealed_pending
    original_read_assembly = assemblies._read_context_assembly_connection
    snapshots, lifecycle = [], []
    phase = {"name": "prepare", "ack_done": False}
    observed = {}

    def open_snapshot(*args, **kwargs):
        snapshot = original_open(*args, **kwargs)
        snapshots.append(snapshot)
        if phase["ack_done"]:
            assert phase["name"] in {"provider", "native"}
            observed[phase["name"]]["snapshots"].append(snapshot)
        return snapshot

    def lifecycle_sink(event):
        assert all(snapshot.closed for snapshot in snapshots)
        lifecycle.append(event.kind)

    def acknowledge(*args, **kwargs):
        receipt = original_ack(*args, **kwargs)
        assert lifecycle == ["committed"]
        assert all(snapshot.closed for snapshot in snapshots)
        phase["ack_done"] = True
        return receipt

    def projection(original, name):
        def project(*args, **kwargs):
            assert phase["ack_done"]
            phase["name"] = name
            observed[name] = {"snapshots": [], "reads": set(), "callbacks": 0}
            result = original(*args, **kwargs)
            assert len(observed[name]["snapshots"]) == 1
            assert observed[name]["snapshots"][0].closed
            phase["name"] = "prepare"
            return result
        return project

    def read(original, label):
        def read_with_snapshot(*args, **kwargs):
            if phase["name"] in observed:
                current = observed[phase["name"]]
                assert len(current["snapshots"]) == 1
                assert kwargs.get("snapshot") is current["snapshots"][0]
                assert not kwargs["snapshot"].closed
                current["reads"].add(label)
                if label == "detail_body":
                    current["reads"].add(kwargs["record"].detail_kind)
            return original(*args, **kwargs)
        return read_with_snapshot

    def read_assembly(connection, *args, **kwargs):
        if phase["name"] in observed:
            current = observed[phase["name"]]
            assert connection is current["snapshots"][0].connection
            assert connection.in_transaction
            current["reads"].add("assembly")
        return original_read_assembly(connection, *args, **kwargs)

    def unlocked(original):
        def callback(*args, **kwargs):
            if phase["name"] in observed:
                current = observed[phase["name"]]
                assert current["snapshots"][0].closed
                current["callbacks"] += 1
            return original(*args, **kwargs)
        return callback

    monkeypatch.setattr(storage, "open_read_snapshot", open_snapshot)
    monkeypatch.setattr(manager, "_lifecycle_event_sink", lifecycle_sink)
    monkeypatch.setattr(manager, "acknowledge_sealed_pending", acknowledge)
    monkeypatch.setattr(assemblies, "_read_context_assembly_connection", read_assembly)
    for owner, name, label in (
        (saver._activation_store(), "find_assembly_binding", "activation"),
        (assemblies, "get_context_plan_detail", "detail_manifest"),
        (saver._detail_store, "read", "detail_body"),
        (storage, "read_items", "items"),
    ):
        monkeypatch.setattr(owner, name, read(getattr(owner, name), label))
    for name, label in (
        ("project_context_plan_to_provider", "provider"),
        ("project_context_plan_to_native", "native"),
    ):
        monkeypatch.setattr(saver, name, projection(getattr(saver, name), label))
    for owner, name in (
        (context_owner, "project_context_plan"),
        (native_request, "project_native_request"),
        (storage, "project_message_attachments"),
        (storage, "project_attachment_payload"),
    ):
        monkeypatch.setattr(owner, name, unlocked(getattr(owner, name)))

    result = prepare("fresh-final", target_format=target_format)
    assert set(observed) == ({"provider", "native"} if target_format == "responses" else {"provider"})
    for current in observed.values():
        assert current["reads"] == {
            "assembly", "activation", "detail_manifest", "detail_body", "items",
            "resource_activation_lineage", "request_source",
        }
        assert current["callbacks"] > 1
    with RolloutCheckpointSaver(storage.sessions_dir) as cold:
        restored = cold.get_context_assembly(session, thread, assembly_id=result["assembly_id"])
        assert cold.project_context_plan_to_provider(
            session, thread, restored.as_sealed_plan(), target_format=target_format,
            include_runtime_notices=True, include_summaries=True,
        ) == (result["messages"], result["tools"], result["losses"])


@pytest.mark.parametrize("damage", ["assembly-manifest", "activation-binding"])
def test_final_projection_rejects_persisted_damage_after_ack(
    production_context, monkeypatch, damage,
):
    saver, _session, _thread, connection, manager, prepare = production_context
    original_ack = manager.acknowledge_sealed_pending
    acknowledged = []

    def acknowledge_then_damage(*args, **kwargs):
        receipt = original_ack(*args, **kwargs)
        acknowledged.append(receipt.assembly_id)
        table, column = (
            ("context_assemblies", "plan_hash") if damage == "assembly-manifest"
            else ("resource_activation_assembly_bindings", "request_hash")
        )
        connection.execute(
            f"UPDATE {table} SET {column}='forged-after-ack' WHERE assembly_id=?",
            (receipt.assembly_id,),
        )
        connection.commit()
        return receipt

    monkeypatch.setattr(manager, "acknowledge_sealed_pending", acknowledge_then_damage)
    expected_error = "context assembly header" if damage == "assembly-manifest" else "resource-activation-hash-mismatch"
    with pytest.raises((ValueError, RuntimeError), match=expected_error):
        prepare("damaged-after-ack")
    assert len(acknowledged) == 1
    assert manager.prepare_pending() is None
    assert connection.execute(
        "SELECT latest_visible_committed_revision FROM context_source_control_states"
    ).fetchone() == ("instructions:1",)
    assert connection.execute("SELECT assembly_id FROM context_assemblies").fetchall() == [(acknowledged[0],)]
    assert saver._prepared_dispatches == {}


@pytest.mark.parametrize("damage", ["unexpected", "nested-value", "missing", "order"])
def test_cold_assembly_rejects_independently_damaged_tool_snapshot(production_context, damage):
    saver, session, thread, connection, _manager, prepare = production_context
    tools = ToolSetSnapshot(
        selection_revision=sha256_jcs("raw-tool-snapshot"),
        tools=() if damage == "unexpected" else tuple({
            "type": "function", "function": {
                "name": name,
                "parameters": {"type": "object", "additionalProperties": False},
            },
        } for name in ("read_file", "write_file")),
    )
    result = prepare("raw-tool-snapshot", supplier=lambda: tools)
    assembly_id = result["assembly_id"]
    original = connection.execute(
        "SELECT snapshot_json, plan_hash, request_hash FROM context_assemblies WHERE assembly_id=?",
        (assembly_id,),
    ).fetchone()
    raw = json.loads(original[0])
    if damage == "unexpected":
        raw["tool_snapshot"].append({"name": "forged-only-snapshot"})
    elif damage == "nested-value":
        raw["tool_snapshot"][0]["function"]["parameters"]["additionalProperties"] = 0
    elif damage == "missing":
        raw["tool_snapshot"].pop()
    else:
        raw["tool_snapshot"].reverse()
    damaged_json = json.dumps(raw, ensure_ascii=False)
    connection.execute(
        "UPDATE context_assemblies SET snapshot_json=? WHERE assembly_id=?",
        (damaged_json, assembly_id),
    )
    connection.commit()
    with (
        RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold,
        pytest.raises(ItemSchemaError, match="tool_snapshot.*ToolSet"),
    ):
        cold.get_context_assembly(session, thread, assembly_id=assembly_id)
    assert connection.execute(
        "SELECT snapshot_json, plan_hash, request_hash FROM context_assemblies WHERE assembly_id=?",
        (assembly_id,),
    ).fetchone() == (damaged_json, *original[1:])


@pytest.mark.parametrize("with_predecessor", [False, True])
@pytest.mark.parametrize("minimal_system", [False, True])
def test_summary_prepare_keeps_canonical_sources_without_applying_pending(
    production_context, with_predecessor, minimal_system,
):
    saver, session, thread, connection, manager, prepare = production_context
    previous = prepare("summary-base") if with_predecessor else None
    manager.observe("instruction:R01", "尚未确认的摘要指引", revision="instructions:2")
    pending = manager.prepare_pending()
    items = saver._storage.storage.read_items(session, thread)
    messages = tuple(project_summary_source(group) for group in summary_source_groups(items).values())
    result = prepare(
        "summary-candidate", request_messages=messages,
        summary_candidate=SummaryCandidateScope(0, "initial", False, minimal_system),
    )
    snapshot = saver.get_context_assembly(session, thread, assembly_id=result["assembly_id"])
    assert snapshot.selection_policy == "summary_candidate"
    assert snapshot.seal_binding.previous_assembly_id is None
    assert snapshot.seal_binding.source_revisions == ()
    assert snapshot.seal_binding.prefix.consumed_transition is None
    backing = [entry for entry in snapshot.selection if entry.ref.ref_type == "canonical_item"]
    assert {entry.ref.ref_id for entry in backing} == {item.item_id for item in items}
    assert all(not entry.included for entry in backing)
    assert [message.content for message in result["messages"][1:]] == [message.content for message in messages]
    expected_system = (
        "Summarize the provided conversation. Return only a concise factual plain-text summary. Do not call tools."
        if minimal_system else "尚未确认的摘要指引"
    )
    assert result["messages"][0].content == expected_system
    assert manager.prepare_pending() == pending
    assert connection.execute(
        "SELECT latest_visible_committed_revision FROM context_source_control_states"
    ).fetchone() == ("instructions:1" if with_predecessor else None,)
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    restored = cold.get_context_assembly(session, thread, assembly_id=result["assembly_id"])
    assert restored.to_dict() == snapshot.to_dict()
    projected, tools, losses = cold.project_context_plan_to_provider(
        session, thread, restored.as_sealed_plan(), target_format="chat_completions",
        include_runtime_notices=True, include_summaries=True,
    )
    assert (projected, tools, losses) == (result["messages"], result["tools"], ())
    with saver._context_reader.open_snapshot(session, thread, "") as read_snapshot:
        latest = saver._storage.assemblies.latest_context_assembly(read_snapshot)
    assert (None if latest is None else latest.assembly_id) == (
        None if previous is None else previous["assembly_id"]
    )


@pytest.mark.parametrize("minimal_system", [False, True])
def test_summary_prepare_rejects_damaged_predecessor_detail(
    production_context, minimal_system,
):
    saver, session, thread, connection, manager, prepare = production_context
    previous = prepare("summary-damaged-base")
    snapshot = saver.get_context_assembly(session, thread, assembly_id=previous["assembly_id"])
    source = next(entry for entry in snapshot.selection if entry.included and entry.ref.ref_type == "request_only")
    path = saver._detail_store._files.path(source.detail_ref)
    assert path is not None
    path.write_bytes(b"{}")
    pending = manager.prepare_pending()
    items = saver._storage.storage.read_items(session, thread)
    messages = tuple(project_summary_source(group) for group in summary_source_groups(items).values())
    with pytest.raises(DetailUnavailableError, match="typed v3"):
        prepare(
            "summary-damaged-candidate", request_messages=messages,
            summary_candidate=SummaryCandidateScope(0, "initial", False, minimal_system),
        )
    assert connection.execute("SELECT assembly_id FROM context_assemblies").fetchall() == [(previous["assembly_id"],)]
    assert manager.prepare_pending() == pending


@pytest.mark.parametrize(
    "change,error,match",
    [
        ("UPDATE context_plans SET revision=revision+1 WHERE plan_id=NEW.plan_id;", RuntimeError, "CAS"),
        ("UPDATE context_plans SET draft_hash='forged' WHERE plan_id=NEW.plan_id;", ValueError, "draft manifest hash"),
        ("UPDATE context_plans SET draft_json='{}' WHERE plan_id=NEW.plan_id;", ValueError, "draft manifest hash"),
        ("DELETE FROM tool_set_snapshots WHERE plan_id=NEW.plan_id;", RuntimeError, "绑定行数"),
    ],
    ids=["revision-cas", "draft-hash-cas", "draft-text-cas", "tool-rowcount"],
)
def test_prepare_rejects_registry_changes_after_preflight(
    production_context, change, error, match,
):
    saver, session, _thread, connection, manager, prepare = production_context
    pending = manager.prepare_pending()
    original_jsonl = saver._storage.storage.jsonl_path(session).read_bytes()
    connection.execute(
        "CREATE TRIGGER change_after_preflight AFTER INSERT ON context_assemblies "
        f"BEGIN {change} END"
    )
    connection.commit()
    with pytest.raises(error, match=match):
        prepare("changed-after-preflight")
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)
    assert connection.execute("SELECT plan_state, revision FROM context_plans").fetchall() == [("unsealed", 0)]
    assert connection.execute(
        "SELECT latest_visible_committed_revision FROM context_source_control_states"
    ).fetchone() == (None,)
    assert manager.prepare_pending() == pending
    assert saver._storage.storage.jsonl_path(session).read_bytes() == original_jsonl


def test_initial_and_delta_use_one_seal_and_exact_prefix(production_context):
    saver, session, thread, connection, manager, prepare = production_context
    first = prepare("first")
    assert [type(message) for message in first["messages"]] == [SystemMessage, HumanMessage]
    assert first["messages"][0].content == "基础规则第一版"
    assert first["tools"] == []
    first_snapshot = saver._storage.assemblies.get_context_assembly(
        session, thread, assembly_id=first["assembly_id"],
    )
    assert len(first_snapshot.tool_set_refs) == 1
    assert connection.execute(
        "SELECT latest_visible_committed_revision FROM context_source_control_states"
    ).fetchone() == ("instructions:1",)
    manager.observe("instruction:R01", "基础规则第二版", revision="instructions:2")
    second = prepare("second")
    second_snapshot = saver._storage.assemblies.get_context_assembly(
        session, thread, assembly_id=second["assembly_id"],
    )
    assert second_snapshot.seal_binding.root == first_snapshot.seal_binding.root
    assert second_snapshot.seal_binding.prefix.state.frames[:2] == first_snapshot.seal_binding.prefix.state.frames
    assert second["messages"][0].content == "基础规则第一版"
    assert isinstance(second["messages"][-1], HumanMessage)
    assert "基础规则第二版" in second["messages"][-1].content
    assert manager.prepare_pending() is None
    assert connection.execute(
        "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchone() == (2,)


def test_failed_seal_never_acknowledges_source(production_context):
    _saver, _session, _thread, connection, manager, prepare = production_context
    batch = manager.prepare_pending()
    connection.execute(
        "CREATE TRIGGER refuse_assembly BEFORE INSERT ON context_assemblies "
        "BEGIN SELECT RAISE(ABORT, 'injected assembly failure'); END"
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected assembly failure"):
        prepare("failure")
    assert manager.prepare_pending() == batch
    assert connection.execute(
        "SELECT latest_visible_committed_revision FROM context_source_control_states"
    ).fetchone() == (None,)
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)


def test_sealed_retry_does_not_read_live_desired(production_context, monkeypatch):
    saver, session, thread, _connection, manager, prepare = production_context
    first = prepare("retry")
    manager.observe("instruction:R01", "尚未发送的新 desired", revision="instructions:2")
    pending = manager.prepare_pending()

    def refuse_desired():
        raise AssertionError("sealed retry 不得重新读取 desired")

    monkeypatch.setattr(manager, "prepare_sources", refuse_desired)
    retry = prepare("retry", supplier=refuse_desired)
    assert retry["assembly_id"] == first["assembly_id"]
    assert retry["messages"] == first["messages"]
    assert pending is not None
    assert tuple(manager._pending.values()) == pending.deltas
    restored = saver._storage.assemblies.get_context_assembly(session, thread, assembly_id=retry["assembly_id"])
    assert restored.seal_binding.source_revisions[0].revision == "instructions:1"


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_late_desired_after_seal_is_preserved_and_next_call_applies_exact_base(
    production_context, monkeypatch, target_format,
):
    saver, _session, _thread, connection, manager, prepare = production_context
    original_seal = saver.seal_context_plan

    def seal_then_observe(*args, **kwargs):
        snapshot = original_seal(*args, **kwargs)
        manager.observe("instruction:R01", "封存后到达的第二版", revision="instructions:2")
        return snapshot

    monkeypatch.setattr(saver, "seal_context_plan", seal_then_observe)
    first = prepare("late-first", target_format=target_format)
    assert first["messages"][0].content == "基础规则第一版"
    pending = manager.prepare_pending()
    assert pending.deltas[0].previous_revision == "instructions:1"
    assert pending.deltas[0].revision == "instructions:2"
    assert connection.execute(
        "SELECT latest_visible_committed_revision, latest_revision FROM context_source_control_states"
    ).fetchone() == ("instructions:1", "instructions:2")
    monkeypatch.setattr(saver, "seal_context_plan", original_seal)
    second = prepare("late-second", target_format=target_format)
    assert second["messages"][0].content == first["messages"][0].content
    assert "封存后到达的第二版" in second["messages"][-1].content
    assert manager.prepare_pending() is None


def test_same_body_new_semantic_revision_is_not_discarded(production_context):
    _saver, _session, _thread, connection, manager, prepare = production_context
    prepare("same-body-first")
    manager.observe("instruction:R01", "基础规则第一版", revision="instructions:2")
    pending = manager.prepare_pending()
    assert pending.deltas[0].revision == "instructions:2"
    second = prepare("same-body-second")
    assert second["messages"][-1].content == "基础规则第一版"
    assert connection.execute(
        "SELECT latest_visible_committed_revision, latest_revision FROM context_source_control_states"
    ).fetchone() == ("instructions:2", "instructions:2")


def test_final_request_is_rechecked_against_sealed_wire(production_context, monkeypatch):
    saver, _session, _thread, _connection, _manager, prepare = production_context
    original = saver.project_context_plan_to_provider

    def drifted_projection(*args, **kwargs):
        messages, tools, losses = original(*args, **kwargs)
        return [*messages, HumanMessage(content="未封存的额外正文")], tools, losses

    monkeypatch.setattr(saver, "project_context_plan_to_provider", drifted_projection)
    with pytest.raises(ValueError, match="最终 Provider request"):
        prepare("wire-drift")
