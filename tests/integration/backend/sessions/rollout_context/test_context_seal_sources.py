"""真实 seal 的同次 canonical 读取、首错顺序与提交完整性合同。"""

from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.assembly.seal_preflight import (
    ContextAssemblySealPreflightError,
)
from app.services.infrastructure.rollout_context.checkpoint.seal import preparation
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 按当前文件隔离正式工作区
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 复用真实 prepare fixture
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.fixture
def tool_messages():
    return (
        AIMessage(
            id="seal-source-assistant", content=[{"type": "text", "text": "读取前说明"}],
            tool_calls=[{"id": "seal-source-call", "name": "read_file", "args": {"path": "a.txt"}}],
        ),
        ToolMessage(
            id="seal-source-result", tool_call_id="seal-source-call",
            name="read_file", content="文件正文", status="success",
        ),
    )


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_wire_seal_reads_canonical_once_and_validates_after_projection(
    production_context, tool_messages, monkeypatch, target_format,
):
    saver, _session, _thread, _connection, _manager, prepare = production_context
    original_seal = preparation.seal_registered_plan
    original_read = saver._storage.storage.read_items
    original_project = saver._project_context_projection
    original_validate = saver._validate_snapshot_sources
    active, reads, projected, validated = [], [], [], []
    events = []

    def seal(*args, **kwargs):
        active.append(True)
        try:
            return original_seal(*args, **kwargs)
        finally:
            active.pop()

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        if active:
            assert kwargs.get("snapshot") is None
            reads.append((tuple(kwargs["item_ids"]), result))
            events.append("read")
        return result

    def project(*args, **kwargs):
        projected.append(args[3])
        events.append("project")
        return original_project(*args, **kwargs)

    def validate(*args, **kwargs):
        validated.append(kwargs["items"])
        events.append("validate")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(preparation, "seal_registered_plan", seal)
    monkeypatch.setattr(saver._storage.storage, "read_items", read)
    monkeypatch.setattr(saver, "_project_context_projection", project)
    monkeypatch.setattr(saver, "_validate_snapshot_sources", validate)
    result = prepare("one-source-read", request_messages=tool_messages, target_format=target_format)
    expected = {
        entry.ref.ref_id for entry in result["snapshot"].selection
        if entry.ref.ref_type == "canonical_item" and entry.included
    }
    assert len(reads) == len(projected) == len(validated) == 1
    assert set(reads[0][0]) == expected == {item.item_id for item in reads[0][1]}
    assert reads[0][1] is projected[0] is validated[0]
    assert {item.semantic_kind for item in validated[0]} >= {"tool_call", "tool_result", "assistant_output"}
    assert events == ["read", "project", "validate"]


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_wire_seal_rejects_nested_payload_mutation_after_first_projection(
    production_context, tool_messages, monkeypatch, target_format,
):
    saver, session, _thread, connection, manager, prepare = production_context
    original_project = saver._project_context_projection
    original_validate = saver._validate_snapshot_sources
    pending = manager.prepare_pending()
    events = []

    def project(*args, **kwargs):
        result = original_project(*args, **kwargs)
        content = next(item for item in args[3] if item.semantic_kind == "assistant_output")
        assert isinstance(content.payload, list)
        content.payload[0]["text"] = "投影后正文已改变"
        events.append("project-mutate")
        return result

    def validate(*args, **kwargs):
        events.append("validate")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(saver, "_project_context_projection", project)
    monkeypatch.setattr(saver, "_validate_snapshot_sources", validate)
    with pytest.raises(ValueError, match="source-mismatch: context plan ref 与 canonical item 不一致"):
        prepare("mutated-local-source", request_messages=tool_messages, target_format=target_format)
    assert events == ["project-mutate", "validate"]
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)
    assert connection.execute("SELECT plan_state FROM context_plans").fetchall() == [("unsealed",)]
    assert manager.prepare_pending() == pending
    assert "投影后正文已改变".encode() not in saver._storage.storage.jsonl_path(session).read_bytes()


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_wire_seal_writer_initialize_rejects_damage_after_projection(
    production_context, monkeypatch, target_format,
):
    saver, session, _thread, connection, manager, prepare = production_context
    storage = saver._storage.storage
    original_project = saver._project_context_projection
    original_validate = saver._validate_snapshot_sources
    original_initialize = storage.initialize
    original_jsonl = storage.jsonl_path(session).read_bytes()
    pending = manager.prepare_pending()
    events = []

    def project(*args, **kwargs):
        result = original_project(*args, **kwargs)
        connection.execute("UPDATE item_catalog SET content_hash='forged-after-projection'")
        connection.commit()
        events.append("project-damage")
        return result

    def validate(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        events.append("source-valid")
        return result

    def initialize(*args, **kwargs):
        if "project-damage" in events:
            assert kwargs.get("validate_jsonl_items", True)
            events.append("writer-initialize")
        return original_initialize(*args, **kwargs)

    monkeypatch.setattr(saver, "_project_context_projection", project)
    monkeypatch.setattr(saver, "_validate_snapshot_sources", validate)
    monkeypatch.setattr(storage, "initialize", initialize)
    with pytest.raises(RuntimeError, match="catalog 与 JSONL identity 不一致"):
        prepare("damaged-durable-source", target_format=target_format)
    assert events[:3] == ["project-damage", "source-valid", "writer-initialize"]
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)
    assert connection.execute("SELECT plan_state FROM context_plans").fetchall() == [("unsealed",)]
    assert manager.prepare_pending() == pending
    assert storage.jsonl_path(session).read_bytes() == original_jsonl


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("missing_prefix", [False, True])
def test_wire_seal_keeps_plan_and_prefix_validation_before_source_read(
    production_context, monkeypatch, target_format, missing_prefix,
):
    _saver, _session, _thread, connection, manager, prepare = production_context
    original_assembly = ContextPlanComposer.assembly
    pending = manager.prepare_pending()

    def assembly(*args, **kwargs):
        snapshot = original_assembly(*args, **kwargs)
        selection = snapshot.selection
        index = next(index for index, entry in enumerate(selection) if entry.ref.ref_type == "canonical_item")
        missing = replace(selection[index], ref=replace(selection[index].ref, ref_id="missing-wire-source"))
        object.__setattr__(snapshot, "refs", tuple(
            missing.ref if ref == selection[index].ref else ref for ref in snapshot.refs
        ))
        object.__setattr__(snapshot, "selection", (*selection[:index], missing, *selection[index + 1:]))
        if missing_prefix:
            object.__setattr__(snapshot, "seal_binding", None)
        else:
            object.__setattr__(snapshot.selection[0], "plan_ordinal", 9)
        return snapshot

    monkeypatch.setattr(ContextPlanComposer, "assembly", assembly)
    error, match = (
        (ValueError, "source-mismatch: wire seal 缺少已绑定 prefix")
        if missing_prefix
        else (ItemSchemaError, "selection.plan_ordinal 必须从 0 连续递增")
    )
    with pytest.raises(error, match=match):
        prepare("wire-first-error", target_format=target_format)
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)
    assert manager.prepare_pending() == pending


@pytest.fixture
def direct_snapshot(registry_db, tool_messages):
    saver, session, _connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    saver.ensure_request_items(
        session, thread, turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"], messages=tool_messages,
    )
    plan = saver.compose_committed_context_plan(
        session, thread, plan_id="direct-source-seal", include_pending_notices=False,
        tool_set=ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
    )
    registered = saver.create_context_plan(
        session, replace(plan, plan_creation_idempotency_key="direct-source-create"),
    )
    return ContextPlanComposer().assembly(
        plan=registered.draft, assembly_id="direct-source-assembly", session_id=session,
        thread_id=thread, turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"], provider_version="direct-source-provider",
    )


def test_direct_snapshot_reads_disjoint_tool_and_remaining_sources(
    registry_db, direct_snapshot, monkeypatch,
):
    saver, session, connection, _accepted = registry_db
    original_read = saver._storage.storage.read_items
    reads = []

    def read(*args, **kwargs):
        reads.append(tuple(kwargs["item_ids"]))
        return original_read(*args, **kwargs)

    monkeypatch.setattr(saver._storage.storage, "read_items", read)
    commit_id = saver.seal_context_assembly(
        session, direct_snapshot.thread_id, direct_snapshot,
        seal_idempotency_key="direct-source-key", seal_input_hash=sha256_jcs("direct-source-input"),
    )
    assert isinstance(commit_id, int)
    assert len(reads) == 2
    assert set(reads[0]).isdisjoint(reads[1])
    assert set(reads[0]) == {
        ref.ref_id for ref in direct_snapshot.refs if ref.semantic_kind in {"tool_call", "tool_result"}
    }
    assert set(reads[0]) | set(reads[1]) == {ref.ref_id for ref in direct_snapshot.refs}
    assert connection.execute("SELECT assembly_id FROM context_assemblies").fetchall() == [(direct_snapshot.assembly_id,)]


@pytest.mark.parametrize("missing_kind", ["tool_call", "tool-pair", "assistant_output"])
def test_direct_snapshot_keeps_tool_read_before_preflight_and_remaining_read_after(
    registry_db, direct_snapshot, monkeypatch, missing_kind,
):
    saver, session, connection, _accepted = registry_db
    selection = direct_snapshot.selection
    index = next(
        index for index, entry in enumerate(selection)
        if entry.ref.semantic_kind == ("tool_call" if missing_kind == "tool-pair" else missing_kind)
    )
    missing = replace(selection[index], ref=replace(selection[index].ref, ref_id="missing-source"))
    object.__setattr__(direct_snapshot, "selection", (*selection[:index], missing, *selection[index + 1:]))
    if missing_kind == "tool-pair":
        result_index = next(index for index, entry in enumerate(selection) if entry.ref.semantic_kind == "tool_result")
        object.__setattr__(
            direct_snapshot.selection[result_index], "ref",
            replace(selection[result_index].ref, ref_id="missing-result"),
        )
    object.__setattr__(direct_snapshot.selection[0], "plan_ordinal", 9)
    original_read = saver._storage.storage.read_items
    reads = []

    def read(*args, **kwargs):
        reads.append(tuple(kwargs["item_ids"]))
        return original_read(*args, **kwargs)

    monkeypatch.setattr(saver._storage.storage, "read_items", read)
    error, match = (
        (KeyError, "canonical item catalog 缺少请求的 item: missing-source")
        if missing_kind in {"tool_call", "tool-pair"}
        else (ContextAssemblySealPreflightError, "seal-dispatch-plan-ordinal-conflict")
    )
    with pytest.raises(error, match=match):
        saver.seal_context_assembly(
            session, direct_snapshot.thread_id, direct_snapshot,
            seal_idempotency_key="invalid-direct-key", seal_input_hash=sha256_jcs("invalid-direct-input"),
        )
    assert len(reads) == 1
    assert ("missing-source" in reads[0]) == (missing_kind in {"tool_call", "tool-pair"})
    if missing_kind == "tool-pair":
        assert reads[0] == ("missing-source", "missing-result")
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)
