"""不可变内容闭包与事务故障；完整恢复/fork由Gateway产品负载另验。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
    snapshot_node_references,
    store_snapshot,
)
from app.services.infrastructure.rollout_context.storage.catalog.unreferenced_content import (
    collect_unreferenced_content,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    store_block,
)
from app.services.infrastructure.rollout_context.storage.checkpoint_state_values import (
    persist_state_value,
)
from tests.integration.backend.sessions.rollout_context.test_assembly_content import (
    shared_assemblies as shared_assemblies,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)
_TABLES = ("checkpoint_state_values", "assembly_content_nodes", "context_membership_items", "context_membership_blocks")


def _rows(connection):
    return {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall() for table in _TABLES}


@pytest.fixture
def gc_case(shared_assemblies):
    saver, session, thread, connection, _manager, first, second, _before = shared_assemblies
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        orphan = first.to_dict()
        orphan["tool_snapshot"] = [{"unused": "没有assembly引用的冻结正文。" * 100}]
        store_snapshot(connection, orphan)
        store_block(connection, [("unreferenced-member", 0, 1, "canonical", 1, 0)])
        persist_state_value(saver._storage.storage, connection, session_id=session, thread_id=thread,
                            checkpoint_ns="", channel="unused-channel", version="unused-version",
                            candidate={"unreferenced": "复杂状态。" * 100}, parent_checkpoint_id=None, is_new_version=True)
    return saver, session, thread, connection, first, second


def test_collects_only_unreferenced_content_and_preserves_frozen_requests(gc_case):
    _saver, session, thread, connection, first, second = gc_case
    roots = [raw for (raw,) in connection.execute("SELECT snapshot_json FROM context_assemblies")]
    kept = set().union(*(snapshot_node_references(connection, raw, owner=(session, thread)) for raw in roots))
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        removed = collect_unreferenced_content(connection)
    assert removed["state_values"] == 1 and removed["assembly_nodes"] > 0
    assert removed["membership_items"] >= 1 and removed["membership_blocks"] >= 1
    assert set(connection.execute("SELECT session_id,thread_id,node_id FROM assembly_content_nodes")) == kept
    assert [raw for (raw,) in connection.execute("SELECT snapshot_json FROM context_assemblies")] == roots
    for original in (first, second):
        raw, = connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (original.assembly_id,)).fetchone()
        assert canonical_json_bytes(load_snapshot(connection, raw)) == canonical_json_bytes(original.to_dict())
        original.validate_hashes()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        assert not any(collect_unreferenced_content(connection).values())


@pytest.mark.parametrize("damage", ["node", "block", "checkpoint"])
def test_invalid_retained_dependency_fails_before_any_deletion(gc_case, damage):
    _saver, _session, _thread, connection, first, _second = gc_case
    with connection:
        if damage == "node":
            raw, = connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (first.assembly_id,)).fetchone()
            key = json.loads(raw)["tool_snapshot"]["node"]
            connection.execute("UPDATE assembly_content_nodes SET body_json='{}' WHERE node_id=?", (key,))
        elif damage == "block":
            connection.execute("UPDATE context_membership_blocks SET content_hash='corrupt' "
                               "WHERE block_id IN (SELECT block_id FROM context_view_item_blocks)")
        else:
            connection.execute("INSERT INTO checkpoint_channels(checkpoint_id,channel_name,storage_kind,value_state,created_at) "
                               "VALUES ('missing-checkpoint','dangling-channel','blob','present','2026-10-21T00:00:00Z')")
    before = _rows(connection)
    with pytest.raises((ValueError, RuntimeError), match="不一致|悬空引用"), connection:
        connection.execute("BEGIN IMMEDIATE")
        collect_unreferenced_content(connection)
    assert _rows(connection) == before


def test_delete_failure_rolls_back_all_collected_families(gc_case):
    _saver, _session, _thread, connection, _first, _second = gc_case
    before = _rows(connection)
    connection.execute("CREATE TEMP TRIGGER fail_gc BEFORE DELETE ON context_membership_blocks "
                       "BEGIN SELECT RAISE(ABORT,'回收发布前中断'); END")
    with pytest.raises(sqlite3.IntegrityError, match="回收发布前中断"), connection:
        connection.execute("BEGIN IMMEDIATE")
        collect_unreferenced_content(connection)
    assert _rows(connection) == before


def test_header_only_canonical_node_is_a_leaf_without_payload_lookup(gc_case):
    _saver, session, thread, connection, first, _second = gc_case
    raw, = connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (first.assembly_id,)).fetchone()
    manifest = json.loads(raw)
    node = {"kind": "canonical", "item_id": "uncaptured-header-only-item", "session_id": session,
            "thread_id": thread, "path": ["text"], "value_hash": sha256_jcs("未捕获正文")}
    key = sha256_jcs(node)
    with connection:
        connection.execute("INSERT INTO assembly_content_nodes VALUES (?,?,?,?)",
                           (session, thread, key, canonical_json_bytes(node).decode()))
    manifest["request_hash_preimage"] = {"node": key}
    encoded = canonical_json_bytes(manifest).decode()
    assert (session, thread, key) in snapshot_node_references(connection, encoded, owner=(session, thread))
    with pytest.raises(ValueError, match="canonical owner/locator"):
        load_snapshot(connection, encoded)
