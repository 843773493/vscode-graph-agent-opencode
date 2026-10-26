"""共享assembly内容的真实prepare/seal、冷读、损坏及显式旧库升级。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.refs import ContextRef
from app.services.infrastructure.rollout_context.assembly import content_store
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
    store_snapshot,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage import (
    rollout_maintenance_owner,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ASSEMBLY_CONTENT_SCHEMA_SQL,
    EXECUTION_QUEUE_SCHEMA_SQL,
    SKILL_SELECTION_MIGRATION_SQL,
    VIEW_MEMBERSHIP_MIGRATION_SQL,
)
from tests.integration.backend.sessions.rollout_context._channel_version_fixtures import (
    restore_schema21,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 按当前测试文件隔离工作区
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 复用真实prepare链
)
from tests.support.e2e_control.harness import E2ETestControlHarness

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.fixture
def shared_assemblies(production_context):
    saver, session, thread, connection, manager, prepare = production_context
    tools = ToolSetSnapshot(selection_revision=sha256_jcs("shared-tools"), tools=({
        "type": "function", "function": {"name": "read_file", "description": "不可变工具定义。" * 100,
                                         "parameters": {"type": "object"}},
    },))
    first = prepare("shared-first", supplier=lambda: tools)["snapshot"]
    before = connection.execute("SELECT count(*) FROM assembly_content_nodes").fetchone()[0]
    second = prepare("shared-second", supplier=lambda: tools)["snapshot"]
    yield saver, session, thread, connection, manager, first, second, before


def test_prepare_shares_tools_and_frozen_prefix_then_cold_reads_without_current_sources(shared_assemblies):
    saver, session, thread, connection, manager, first, second, before = shared_assemblies
    manifests = [json.loads(connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (s.assembly_id,)).fetchone()[0]) for s in (first, second)]
    assert manifests[0]["tool_snapshot"] == manifests[1]["tool_snapshot"]
    assert manifests[0]["_storage_format"] == "assembly_content_v1"
    assert len(canonical_json_bytes(manifests[0])) < len(canonical_json_bytes(first.to_dict()))
    assert connection.execute("SELECT count(*) FROM assembly_content_nodes").fetchone()[0] > before
    # 当前源改变后，历史请求仍只使用封存节点，不能重新调用producer猜测旧输入。
    manager.observe("instruction:R01", "当前规则已经改变", revision="instructions:changed")
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    for original in (first, second):
        restored = cold.get_context_assembly(session, thread, assembly_id=original.assembly_id)
        restored.validate_hashes()
        assert restored.to_dict() == original.to_dict()


@pytest.fixture
def assembly_control(shared_assemblies, tmp_path):
    saver, session, thread, _connection, _manager, first, second, _before = shared_assemblies
    control = E2ETestControlHarness(tmp_path / "control")
    try:
        control.bind_trace_root("owner", saver._storage.storage.root(session, thread_id=thread))
        yield control, {
            "action": "read_assemblies", "name": "owner", "relative_path": "index.sqlite",
            "session_id": session, "thread_id": thread,
            "model_call_ids": [second.model_call_id, first.model_call_id],
        }
    finally:
        control.close()


def test_browser_observer_materializes_frozen_snapshots_and_preserves_raw_bytes(
    assembly_control, shared_assemblies,
):
    control, request = assembly_control
    _saver, _session, _thread, connection, manager, first, second, _before = shared_assemblies
    raw = dict(connection.execute("SELECT model_call_id,snapshot_json FROM context_assemblies"))
    manager.observe("instruction:R01", "当前规则不属于历史请求", revision="instructions:changed")

    observed = control._dispatch(request)

    assert [row["call"] for row in observed] == request["model_call_ids"]
    for row, original in zip(observed, (second, first), strict=True):
        assert row["bytes"] == raw[original.model_call_id]
        assert json.loads(row["bytes"])["_storage_format"] == "assembly_content_v1"
        assert canonical_json_bytes(row["snapshot"]) == canonical_json_bytes(original.to_dict())
    assert dict(connection.execute("SELECT model_call_id,snapshot_json FROM context_assemblies")) == raw


@pytest.mark.parametrize("scope", ["session_id", "thread_id"])
def test_browser_observer_rejects_other_owner_without_rewriting_history(
    assembly_control, shared_assemblies, scope,
):
    control, request = assembly_control
    _saver, _session, _thread, connection, _manager, _first, _second, _before = shared_assemblies
    before = connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall()
    with pytest.raises(LookupError, match="当前SessionThread缺少sealed assembly"):
        control._dispatch({**request, scope: "other-owner"})
    assert connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall() == before


def test_browser_observer_exposes_missing_content_without_repair(assembly_control, shared_assemblies):
    control, request = assembly_control
    _saver, _session, _thread, connection, _manager, first, _second, _before = shared_assemblies
    raw = connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (first.assembly_id,)).fetchone()[0]
    key = json.loads(raw)["tool_snapshot"]["node"]
    connection.execute("DELETE FROM assembly_content_nodes WHERE node_id=?", (key,))
    connection.commit()
    before = connection.execute("SELECT * FROM assembly_content_nodes ORDER BY node_id").fetchall()

    with pytest.raises(ValueError, match="assembly content 缺失"):
        control._dispatch(request)

    assert connection.execute("SELECT * FROM assembly_content_nodes ORDER BY node_id").fetchall() == before


@pytest.mark.parametrize("damage", ["missing", "hash", "owner"])
def test_shared_node_damage_fails_without_repair(shared_assemblies, damage):
    saver, session, thread, connection, _manager, first, _second, _before = shared_assemblies
    raw = connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (first.assembly_id,)).fetchone()[0]
    key = json.loads(raw)["tool_snapshot"]["node"]
    if damage == "missing":
        connection.execute("DELETE FROM assembly_content_nodes WHERE node_id=?", (key,))
    elif damage == "hash":
        connection.execute("UPDATE assembly_content_nodes SET body_json='{}' WHERE node_id=?", (key,))
    else:
        connection.execute("UPDATE assembly_content_nodes SET thread_id='other-owner' WHERE node_id=?", (key,))
    connection.commit()
    before = connection.execute("SELECT * FROM assembly_content_nodes ORDER BY node_id").fetchall()
    with pytest.raises(ValueError, match="assembly content.*缺失|assembly content hash"):
        RolloutCheckpointSaver(saver._storage.storage.sessions_dir).get_context_assembly(session, thread, assembly_id=first.assembly_id)
    assert connection.execute("SELECT * FROM assembly_content_nodes ORDER BY node_id").fetchall() == before


@pytest.mark.parametrize("row_factory", [None, sqlite3.Row])
def test_large_canonical_payload_is_referenced_once_and_exact_owner_is_required(
    production_context, row_factory, monkeypatch,
):
    saver, session, thread, connection, _manager, _prepare = production_context
    body = "唯一canonical正文，不能复制成第二份消息历史。" * 100
    payload = {"first": body, "nested": [{"second": body}]}
    accepted = saver.accept_turn(session, thread_id=thread, accepted_ingress_id="large-body", acceptance_idempotency_key="large-body-accept", job_id="large-body-job", admission_idempotency_key="large-body-admit", payload=payload, payload_kind="structured_content")
    item, = saver._storage.storage.read_items(session, thread, item_ids=(accepted["root_input_item_id"],))
    ref = ContextRef.canonical_item(item, session_id=session, thread_id=thread).to_dict()
    value = {"session_id": session, "thread_id": thread, "selection": [{"ref": ref, "included": True}],
             "request_hash_preimage": {"messages": [{"role": "user", "content": body}]},
             "tool_snapshot": [], "tool_set_refs": [], "seal_binding": None}
    body_hash_calls = 0
    original_hash = content_store.sha256_jcs

    def count_body_hash(candidate):
        nonlocal body_hash_calls
        if candidate == body:
            body_hash_calls += 1
        return original_hash(candidate)

    monkeypatch.setattr(content_store, "sha256_jcs", count_body_hash)
    connection.execute("BEGIN IMMEDIATE")
    original_factory = connection.row_factory
    connection.row_factory = row_factory
    encoded = store_snapshot(connection, value)
    assert body_hash_calls == 1
    dependencies: set[str] = set()
    assert load_snapshot(connection, encoded, canonical_item_ids=dependencies) == value
    assert dependencies == {item.item_id}
    nodes = [row[0] for row in connection.execute("SELECT body_json FROM assembly_content_nodes")]
    assert not any(body in node for node in nodes)
    canonical_sources = [json.loads(node) for node in nodes if json.loads(node)["kind"] == "canonical"]
    assert canonical_sources == [{
        "kind": "canonical", "item_id": item.item_id, "session_id": session,
        "thread_id": thread, "path": ["first"], "value_hash": original_hash(body),
    }]
    connection.row_factory = original_factory
    connection.rollback()


def test_schema21_upgrade_preserves_historical_jcs_hashes_and_jsonl(shared_assemblies):
    saver, session, thread, connection, _manager, first, second, _before = shared_assemblies
    restore_schema21(connection)
    old = connection.execute("SELECT assembly_id,snapshot_json FROM context_assemblies ORDER BY assembly_id").fetchall()
    jsonl = saver._storage.storage.jsonl_path(session, thread_id=thread)
    before = jsonl.read_bytes()
    saver._storage.storage.migrate_schema(thread, session_id=session, to_version=22,
                                         migration_name="assembly_shared_content", migration_sql=ASSEMBLY_CONTENT_SCHEMA_SQL)
    assert [(key, canonical_json_bytes(load_snapshot(connection, raw)).decode()) for key, raw in connection.execute("SELECT assembly_id,snapshot_json FROM context_assemblies ORDER BY assembly_id")] == old
    assert jsonl.read_bytes() == before
    assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (22, "active")
    saver._storage.storage.migrate_schema(thread, session_id=session, to_version=23,
                                         migration_name="view_membership", migration_sql=VIEW_MEMBERSHIP_MIGRATION_SQL)
    saver._storage.storage.migrate_schema(thread, session_id=session, to_version=24,
                                         migration_name="execution_queue_023_024", migration_sql=EXECUTION_QUEUE_SCHEMA_SQL)
    saver._storage.storage.migrate_schema(thread, session_id=session, to_version=25,
                                         migration_name="skill_selection_024_025", migration_sql=SKILL_SELECTION_MIGRATION_SQL)
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    for original in (first, second):
        assert cold.get_context_assembly(session, thread, assembly_id=original.assembly_id).to_dict() == original.to_dict()


def test_interrupted_content_migration_restores_old_manifest_and_backup(shared_assemblies, monkeypatch):
    saver, session, thread, connection, _manager, _first, _second, _before = shared_assemblies
    restore_schema21(connection)
    old = connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall()
    migrate = rollout_maintenance_owner.migrate_assembly_content

    def interrupted(db):
        migrate(db)
        raise RuntimeError("内容迁移已转引用后中断")

    monkeypatch.setattr(rollout_maintenance_owner, "migrate_assembly_content", interrupted)
    with pytest.raises(RuntimeError, match="转引用后中断"):
        saver._storage.storage.migrate_schema(thread, session_id=session, to_version=22,
                                             migration_name="assembly_shared_content", migration_sql=ASSEMBLY_CONTENT_SCHEMA_SQL)
    assert connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall() == old
    assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (21, "recovery_required")
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='assembly_content_nodes'").fetchone() is None
    assert list(saver._storage.storage.root(session, thread_id=thread).glob("index.sqlite.migration-*.backup"))


def test_shared_nodes_restore_independent_json_fields(shared_assemblies):
    _saver, _session, _thread, connection, _manager, first, _second, _before = shared_assemblies
    raw = connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (first.assembly_id,)).fetchone()[0]
    decoded = load_snapshot(connection, raw)
    original = canonical_json_bytes(decoded["tool_set_refs"])
    decoded["tool_snapshot"][0]["function"]["description"] = "只编辑这个字段"
    assert canonical_json_bytes(decoded["tool_set_refs"]) == original
    assert canonical_json_bytes(load_snapshot(connection, raw)) == canonical_json_bytes(first.to_dict())


def test_seal_failure_rolls_back_content_nodes_and_assembly(production_context, monkeypatch):
    from app.services.infrastructure.rollout_context.assembly import sealing

    saver, session, thread, connection, _manager, prepare = production_context
    first = prepare("before-failed-publish")["snapshot"]
    nodes = connection.execute("SELECT * FROM assembly_content_nodes ORDER BY node_id").fetchall()
    assemblies = connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall()
    bind = sealing.bind_sealed_registration

    def interrupted(db, seal):
        bind(db, seal)
        raise RuntimeError("封存节点发布前中断")

    monkeypatch.setattr(sealing, "bind_sealed_registration", interrupted)
    with pytest.raises(RuntimeError, match="封存节点发布前中断"):
        prepare("failed-publish")
    assert connection.execute("SELECT * FROM assembly_content_nodes ORDER BY node_id").fetchall() == nodes
    assert connection.execute("SELECT * FROM context_assemblies ORDER BY assembly_id").fetchall() == assemblies
    assert saver.get_context_assembly(session, thread, assembly_id=first.assembly_id).to_dict() == first.to_dict()
