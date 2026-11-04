"""共享assembly内容的真实prepare/seal、冷读、损坏及显式旧库升级。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.assembly import content_store
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
    store_snapshot,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.storage import (
    rollout_maintenance_owner,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ASSEMBLY_CONTENT_SCHEMA_SQL,
    EXECUTION_QUEUE_SCHEMA_SQL,
    SKILL_SELECTION_MIGRATION_SQL,
    VIEW_CREATION_MIGRATION_SQL,
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
from tests.harness.python.run_context import TestRunContext

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.fixture
def canonical_files(monkeypatch):
    opened = []
    open_path = Path.open

    def observe_open(path, *args, **kwargs):
        stream = open_path(path, *args, **kwargs)
        if path.name == "rollout.jsonl" and sys._getframe(1).f_code.co_filename == content_store.__file__:
            opened.append(stream)
        return stream

    monkeypatch.setattr(Path, "open", observe_open)
    return opened


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


def test_prepare_shares_tools_and_frozen_prefix_then_cold_reads_without_current_sources(
    shared_assemblies, canonical_files,
):
    saver, session, thread, connection, manager, first, second, before = shared_assemblies
    manifests = [json.loads(connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (s.assembly_id,)).fetchone()[0]) for s in (first, second)]
    assert manifests[0]["tool_snapshot"] == manifests[1]["tool_snapshot"]
    assert manifests[0]["_storage_format"] == "assembly_content_v1"
    assert len(canonical_json_bytes(manifests[0])) < len(canonical_json_bytes(first.to_dict()))
    assert connection.execute("SELECT count(*) FROM assembly_content_nodes").fetchone()[0] > before
    # 当前源改变后，历史请求仍只使用封存节点，不能重新调用producer猜测旧输入。
    manager.observe("instruction:R01", "当前规则已经改变", revision="instructions:changed")
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for original in (first, second):
            restored = cold.get_context_assembly(session, thread, assembly_id=original.assembly_id)
            restored.validate_hashes()
            assert restored.to_dict() == original.to_dict()
    assert canonical_files == []


def test_cold_assembly_fetches_snapshot_body_once(shared_assemblies, monkeypatch, request):
    saver, session, thread, connection, _manager, first, _second, _before = shared_assemblies
    body_reads = 0

    def observe_read(action, table, column, _database, _trigger):
        nonlocal body_reads
        if action == sqlite3.SQLITE_READ and (table, column) == ("context_assemblies", "snapshot_json"):
            body_reads += 1
        return sqlite3.SQLITE_OK

    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        connect = cold._storage.storage._connect

        def observed_connection(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_authorizer(observe_read)
            return db

        monkeypatch.setattr(cold._storage.storage, "_connect", observed_connection)
        restored = cold.get_context_assembly(session, thread, assembly_id=first.assembly_id)
        assert restored.to_dict() == first.to_dict()
    body_bytes = connection.execute(
        "SELECT length(CAST(snapshot_json AS BLOB)) FROM context_assemblies WHERE assembly_id=?",
        (first.assembly_id,),
    ).fetchone()[0]
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "cold-snapshot-body-reads.json").write_text(
        json.dumps({"snapshot_bytes": body_bytes, "body_column_reads": body_reads}, indent=2), encoding="utf-8",
    )
    # header 只消费标量；整份历史正文只交给 registry 读取与校验一次。
    assert body_reads == 1


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


@pytest.mark.parametrize("damage", ["missing", "hash", "owner", "noncanonical"])
def test_shared_node_damage_fails_without_repair(shared_assemblies, damage):
    saver, session, thread, connection, _manager, first, _second, _before = shared_assemblies
    raw = connection.execute("SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (first.assembly_id,)).fetchone()[0]
    key = json.loads(raw)["tool_snapshot"]["node"]
    if damage == "missing":
        connection.execute("DELETE FROM assembly_content_nodes WHERE node_id=?", (key,))
    elif damage == "hash":
        connection.execute("UPDATE assembly_content_nodes SET body_json='{}' WHERE node_id=?", (key,))
    elif damage == "noncanonical":
        connection.execute("UPDATE assembly_content_nodes SET body_json=' ' || body_json WHERE node_id=?", (key,))
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


@pytest.mark.parametrize("item_count", [1, 4])
def test_cold_assembly_shares_one_canonical_file_and_closes_after_damage(
    production_context, frozen_provider_wire, frozen_provider_activation, monkeypatch, item_count, canonical_files,
):
    saver, session, thread, connection, _manager, _prepare = production_context
    expected_items = set()
    for index in range(item_count):
        accepted = saver.accept_turn(
            session, thread_id=thread,
            accepted_ingress_id=f"canonical-file:{index}",
            acceptance_idempotency_key=f"canonical-file-accept:{index}",
            job_id=f"canonical-file-job:{index}", admission_idempotency_key=f"canonical-file-admit:{index}",
            payload=f"canonical-file-body:{index}:" + "不可变的长历史正文。" * 40, payload_kind="text",
        )
        expected_items.add(accepted["root_input_item_id"])
    admission = saver.execution_admission(session, thread_id=thread, execution_id=accepted["initial_execution_id"])
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        prepared = saver.prepare_context_for_provider(
            session, thread, turn_id=accepted["turn_id"], execution_id=admission.execution_id,
            plan_creation_idempotency_key="canonical-file-create", seal_idempotency_key="canonical-file-seal",
            context_source_manager=ContextSourceManager(owner=ContextSourceOwnerKey(session, thread), control_state_port=saver),
            tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
            **frozen_provider_wire("canonical-file-provider", "chat_completions"),
            **frozen_provider_activation(saver, plan_creation_idempotency_key="canonical-file-create"),
        )
    original = prepared["snapshot"]
    captured = {value["item_id"] for row in connection.execute("SELECT body_json FROM assembly_content_nodes")
                if (value := json.loads(row[0]))["kind"] == "canonical"}
    # 从真实 prepare 取得引用，不手工给内容 reader 填 canonical metadata。
    assert captured == expected_items
    opened = canonical_files
    opened.clear()
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        restored = cold.get_context_assembly(session, thread, assembly_id=original.assembly_id)
        assert restored.to_dict() == original.to_dict()
        assert len(opened) == 1
        assert all(stream.closed for stream in opened)
        opened.clear()
        jsonl = saver._storage.storage.jsonl_path(session, thread_id=thread)
        raw = jsonl.read_bytes()
        damaged = raw.replace(b"canonical-file-body:0:", b"canonical-file-body:X:", 1)
        assert damaged != raw
        try:
            jsonl.write_bytes(damaged)
            with pytest.raises(RuntimeError, match="v2 item JSONL schema/content 非法"):
                cold.get_context_assembly(session, thread, assembly_id=original.assembly_id)
            assert len(opened) == 1
            assert all(stream.closed for stream in opened)
            assert jsonl.read_bytes() == damaged
        finally:
            jsonl.write_bytes(raw)

    opened.clear()
    # 封存入口传入 JSON 解码值；DTO 中的 tuple 先按相同序列化边界还原。
    snapshot_value = json.loads(canonical_json_bytes(original.to_dict()))
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        encoded = store_snapshot(connection, snapshot_value)
    assert len(opened) == 1
    assert all(stream.closed for stream in opened)
    assert encoded == connection.execute(
        "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (original.assembly_id,),
    ).fetchone()[0]
    opened.clear()
    assert content_store.snapshot_node_references(connection, encoded, owner=(session, thread))
    assert opened == []
    nodes = connection.execute("SELECT * FROM assembly_content_nodes ORDER BY node_id").fetchall()
    encode = content_store._AssemblyContentStore.encode

    def interrupted(store, value):
        encode(store, value)
        raise RuntimeError("canonical 文件打开后中断封存")

    monkeypatch.setattr(content_store._AssemblyContentStore, "encode", interrupted)
    with pytest.raises(RuntimeError, match="canonical 文件打开后中断封存"), connection:
        connection.execute("BEGIN IMMEDIATE")
        store_snapshot(connection, snapshot_value)
    assert len(opened) == 1
    assert all(stream.closed for stream in opened)
    assert connection.execute("SELECT * FROM assembly_content_nodes ORDER BY node_id").fetchall() == nodes
    assert jsonl.read_bytes() == raw


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

    saver._storage.storage.migrate_schema(
        thread, session_id=session, to_version=26,
        migration_name="view_creation_025_026", migration_sql=VIEW_CREATION_MIGRATION_SQL,
    )
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


def _container_count(value: object) -> int:
    if isinstance(value, dict):
        return 1 + sum(_container_count(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return 1 + sum(_container_count(child) for child in value)
    return 0


@pytest.mark.parametrize("kind", ["string", "array", "object", "escaped_key"])
@pytest.mark.parametrize("size", [128, 129])
def test_content_inline_boundary_keeps_exact_manifest_and_node_bytes(production_context, kind, size):
    _saver, session, thread, connection, _manager, _prepare = production_context
    factories = {
        "string": lambda body: body,
        "array": lambda body: [body],
        "object": lambda body: {"k": body},
        "escaped_key": lambda body: {"😀\n\"\\": body},
    }
    factory = factories[kind]
    body = "x" * (size - len(canonical_json_bytes(factory(""))))
    value = factory(body)
    assert len(canonical_json_bytes(value)) == size
    snapshot = {
        "session_id": session, "thread_id": thread, "selection": [],
        "request_hash_preimage": {}, "tool_snapshot": value, "tool_set_refs": [], "seal_binding": None,
    }
    nodes = set(connection.execute("SELECT node_id,body_json FROM assembly_content_nodes"))
    connection.execute("BEGIN IMMEDIATE")
    encoded = store_snapshot(connection, snapshot)
    if size == 128:
        reference = {"literal": value}
    else:
        if kind == "string":
            node = {"kind": "value", "value": value}
        elif kind == "array":
            node = {"kind": "prefix", "parent": None, "value": {"literal": body}}
        else:
            node = {"kind": "object", "entries": {key: {"literal": body} for key in value}}
        key = sha256_jcs(node)
        nodes.add((key, canonical_json_bytes(node).decode("utf-8")))
        reference = {"node": key}
    expected = {
        **snapshot, "_storage_format": "assembly_content_v1", "tool_snapshot": reference,
        "request_hash_preimage": {"literal": {}}, "tool_set_refs": {"literal": []},
        "seal_binding": {"literal": None},
    }
    assert encoded == canonical_json_bytes(expected).decode("utf-8")
    assert set(connection.execute("SELECT node_id,body_json FROM assembly_content_nodes")) == nodes
    assert canonical_json_bytes(load_snapshot(connection, encoded)) == canonical_json_bytes(snapshot)
    connection.rollback()


@pytest.mark.parametrize("depth", [4, 20])
def test_nested_tools_cold_read_copies_only_linear_content(
    production_context, monkeypatch, request, depth,
):
    saver, session, thread, connection, _manager, prepare = production_context
    leaf = {"type": "string", "enum": ["alpha", "beta"]}
    schema = {"type": "object", "properties": {"left": leaf, "right": leaf}}
    for _ in range(depth):
        schema = {
            "type": "object", "properties": {"next": schema},
            "description": "嵌套参数说明。" * 30,
        }
    tools = ToolSetSnapshot(selection_revision=sha256_jcs(schema), tools=tuple(
        {"type": "function", "function": {"name": name, "parameters": schema}}
        for name in ("read_nested", "write_nested")
    ))
    encoded_containers = 0
    original_encode = content_store.canonical_json_bytes

    def count_encode(value):
        nonlocal encoded_containers
        encoded_containers += _container_count(value)
        return original_encode(value)

    with monkeypatch.context() as patch:
        patch.setattr(content_store, "canonical_json_bytes", count_encode)
        snapshot = prepare(f"nested-tools:{depth}", supplier=lambda: tools)["snapshot"]
    expected = snapshot.to_dict()
    copy_containers = 0
    original_copy = content_store.deepcopy

    def count_copy(value):
        nonlocal copy_containers
        copy_containers += _container_count(value)
        return original_copy(value)

    # 经过真实冷 registry/assembly reader，防止只测 helper 而漏掉实际调用。
    with monkeypatch.context() as patch, RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        patch.setattr(content_store, "deepcopy", count_copy)
        restored = cold.get_context_assembly(session, thread, assembly_id=snapshot.assembly_id)
        assert restored.to_dict() == expected
    containers = _container_count(expected)
    measurement = {"depth": depth, "output_containers": containers, "copied_containers": copy_containers,
                   "encoded_containers": encoded_containers}
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"nested-tools-copy-{depth}.json").write_text(
        json.dumps(measurement, indent=2), encoding="utf-8",
    )
    print(f"nested tools cold read: {measurement}")

    raw = connection.execute(
        "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (snapshot.assembly_id,),
    ).fetchone()[0]
    decoded = load_snapshot(connection, raw)
    other_fields = canonical_json_bytes({key: value for key, value in decoded.items() if key != "tool_snapshot"})
    first, second = decoded["tool_snapshot"]
    other_tool = canonical_json_bytes(second)
    parameters = first["function"]["parameters"]
    for _ in range(depth):
        parameters = parameters["properties"]["next"]
    parameters["properties"]["left"]["enum"].append("changed")
    assert parameters["properties"]["right"]["enum"] == ["alpha", "beta"]
    assert canonical_json_bytes(second) == other_tool
    assert canonical_json_bytes({key: value for key, value in decoded.items() if key != "tool_snapshot"}) == other_fields
    assert canonical_json_bytes(load_snapshot(connection, raw)) == canonical_json_bytes(expected)
    # 复制量应随返回 JSON 大小线性增长，不能随嵌套层数反复复制已构建子树。
    assert copy_containers <= containers * 2
    # 统计 content_store 的内联判定与 manifest 编码，不包含节点 hash 的独立编码。
    assert encoded_containers <= containers * 2


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
