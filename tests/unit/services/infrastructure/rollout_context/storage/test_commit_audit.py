"""完整审计沿主键顺序流式读取，限制历史增长带来的排序与正文内存。"""

from __future__ import annotations

import json
import sqlite3
import tracemalloc
from pathlib import Path
from time import perf_counter

import pytest

from app.core.identifier import create_uuid_hex
from app.domain.itemized.enums import CommitKind, CommitMode, ControlOutcome
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage import rollout_schema_introspection
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def audit_rollout(tmp_path: Path, session_bundle_factory):
    sessions = tmp_path / ".boxteam/sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    storage = RolloutContextStore(sessions).storage
    thread_id = storage.main_thread_id(session_id)
    storage.initialize(session_id, thread_id=thread_id)
    # 经真实 coordinator 一次提交多条正文，测量审计本身，不计造数和 fsync。
    items = tuple(CanonicalItemRecord.create(
        item_sequence=index, item_id=f"audit-{index}", semantic_kind="user_input",
        payload_kind="text", status="completed", payload="x" * (32 * 1024),
        producer_ref={"producer_kind": "user", "producer_id": "audit-input"},
    ) for index in range(1, 257))
    with storage._lock(thread_id, "", session_id=session_id):
        with storage._connect(thread_id, session_id=session_id) as connection:
            storage._append_v2_records_transaction(
                connection, thread_id, "", items, session_id=session_id,
                commit_kind="item_convergence",
            )
            storage._commit_connection(connection)
    return storage, session_id, thread_id


def test_warm_initialization_does_not_buffer_entire_committed_history(audit_rollout, request):
    storage, session_id, thread_id = audit_rollout
    storage.initialize(session_id, thread_id=thread_id)
    elapsed = []
    for _ in range(3):
        started = perf_counter()
        storage.initialize(session_id, thread_id=thread_id)
        elapsed.append(perf_counter() - started)
    tracemalloc.start()
    try:
        storage.initialize(session_id, thread_id=thread_id)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    jsonl_bytes = storage.jsonl_path(session_id, thread_id=thread_id).stat().st_size
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "audit-measurements.json").write_text(json.dumps({
        "items": 256, "jsonl_bytes": jsonl_bytes, "warm_seconds": elapsed,
        "peak_traced_bytes": peak,
    }, indent=2), encoding="utf-8")
    assert peak < jsonl_bytes // 2, (peak, jsonl_bytes)


def test_snapshot_catalog_audit_does_not_sort_all_historical_locators(audit_rollout, monkeypatch):
    storage, session_id, thread_id = audit_rollout
    connect = storage._connect
    statements = []

    def observe(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(storage, "_connect", observe)
    with storage.open_read_snapshot(session_id, thread_id) as snapshot:
        assert snapshot.session_id == session_id
    # commit 数量校验应消费同一次 catalog 顺序流，不应再全表 GROUP BY 一遍。
    catalog_queries = [statement for statement in statements if "FROM item_catalog" in statement]
    assert len(catalog_queries) == 1
    with storage._connect(thread_id, session_id=session_id, read_only=True) as connection:
        plan = connection.execute("EXPLAIN QUERY PLAN " + catalog_queries[0]).fetchall()
    assert not any("TEMP B-TREE FOR ORDER BY" in row[3] for row in plan), plan


@pytest.mark.parametrize("operation", ["initialize", "snapshot"])
def test_audit_rejects_reordered_locators_without_reclaiming_tail(audit_rollout, operation):
    storage, session_id, thread_id = audit_rollout
    storage.initialize(session_id, thread_id=thread_id)
    jsonl = storage.jsonl_path(session_id, thread_id=thread_id)
    with jsonl.open("ab") as stream:
        stream.write(b"uncommitted-tail")
    before = jsonl.read_bytes()
    with sqlite3.connect(storage.index_path(session_id, thread_id=thread_id)) as connection:
        first, second = connection.execute(
            "SELECT jsonl_offset FROM item_catalog ORDER BY item_sequence LIMIT 2",
        ).fetchall()
        connection.executemany("UPDATE item_catalog SET jsonl_offset=? WHERE item_sequence=?", [
            (second[0], 1), (first[0], 2),
        ])
    with pytest.raises(RuntimeError, match="item_sequence 不连续|locator 不连续"):
        if operation == "initialize":
            storage.initialize(session_id, thread_id=thread_id)
        else:
            with storage.open_read_snapshot(session_id, thread_id):
                pytest.fail("不能接受正文位置与 item 顺序不一致的历史")
    assert jsonl.read_bytes() == before


@pytest.fixture
def commit_history_factory(tmp_path: Path, session_bundle_factory):
    sessions = tmp_path / ".boxteam/sessions"
    storage = RolloutContextStore(sessions).storage

    def create(commit_count: int):
        session_id = f"ses_{create_uuid_hex()}"
        session_bundle_factory(sessions, session_id)
        thread_id = storage.main_thread_id(session_id)
        storage.initialize(session_id, thread_id=thread_id)
        item_sequence = 0
        with storage._lock(thread_id, "", session_id=session_id):
            with storage._connect(thread_id, session_id=session_id) as connection:
                for index in range(commit_count):
                    items = ()
                    if index % (commit_count // 32) == 0:
                        item_sequence += 1
                        items = (CanonicalItemRecord.create(
                            item_sequence=item_sequence, item_id=f"audit-{item_sequence}",
                            semantic_kind="user_input", payload_kind="text", status="completed", payload="audit",
                            producer_ref={"producer_kind": "user", "producer_id": "audit-input"},
                        ),)
                    storage._append_v2_records_transaction(
                        connection, thread_id, "", items, session_id=session_id,
                        commit_kind="item_convergence" if items else "assembly_sealed",
                        commit_mode="item_bearing" if items else "metadata_only",
                        idempotency_key=f"audit-commit-{index}", begin_transaction=index == 0,
                    )
                storage._commit_connection(connection)
        assert item_sequence == 32
        return storage, session_id, thread_id

    return create


def test_snapshot_audit_does_not_retain_all_commit_boundaries(commit_history_factory, request):
    measurements = []
    for commits in (64, 4096):
        storage, session_id, thread_id = commit_history_factory(commits)
        tracemalloc.start()
        try:
            started = perf_counter()
            with storage.open_read_snapshot(session_id, thread_id):
                pass
            elapsed = perf_counter() - started
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        measurements.append({"commits": commits, "items": 32, "seconds": elapsed, "peak_traced_bytes": peak})
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "commit-audit-memory.json").write_text(json.dumps(measurements, indent=2), encoding="utf-8")
    print(f"commit audit memory: {measurements}")
    assert measurements[1]["peak_traced_bytes"] < measurements[0]["peak_traced_bytes"] + 128 * 1024


@pytest.mark.parametrize("operation", ["initialize", "snapshot"])
def test_commit_audit_reuses_closed_enums_while_rechecking_every_record(
    commit_history_factory, monkeypatch, operation,
):
    storage, session_id, thread_id = commit_history_factory(4096)
    with storage._lock(thread_id, "", session_id=session_id):
        with storage._connect(thread_id, session_id=session_id) as connection:
            storage._append_v2_records_transaction(
                connection, thread_id, "", (), session_id=session_id,
                commit_kind="terminal_convergence", commit_mode="metadata_only",
                outcome="completed_empty", idempotency_key="audit-terminal",
            )
            storage._commit_connection(connection)
    scans = {CommitKind: 0, CommitMode: 0, ControlOutcome: 0}
    enum_iter = type(CommitKind).__iter__
    contract = rollout_schema_introspection.validate_commit_contract
    checked = 0

    def observe_enum(enum):
        if enum in scans:
            scans[enum] += 1
        return enum_iter(enum)

    def observe_contract(**values):
        nonlocal checked
        checked += 1
        return contract(**values)

    monkeypatch.setattr(type(CommitKind), "__iter__", observe_enum)
    monkeypatch.setattr(rollout_schema_introspection, "validate_commit_contract", observe_contract)
    if operation == "initialize":
        storage.initialize(session_id, thread_id=thread_id)
    else:
        with storage.open_read_snapshot(session_id, thread_id):
            pass
    print(f"commit audit enum scans ({operation}): {[(enum.__name__, count) for enum, count in scans.items()]}; checked={checked}")
    assert checked == 4097
    assert not any(scans.values())


def test_valid_mixed_commits_validate_bodies_and_reclaim_only_uncommitted_tail(commit_history_factory):
    storage, session_id, thread_id = commit_history_factory(64)
    jsonl = storage.jsonl_path(session_id, thread_id=thread_id)
    committed = jsonl.read_bytes()
    with jsonl.open("ab") as stream:
        stream.write(b"uncommitted-tail")
    storage.initialize(session_id, thread_id=thread_id)
    assert jsonl.read_bytes() == committed
    with storage.open_read_snapshot(session_id, thread_id, validate_integrity=True) as snapshot:
        assert snapshot.session_id == session_id


@pytest.mark.parametrize("operation", ["initialize", "snapshot"])
@pytest.mark.parametrize("corruption", [
    "UPDATE storage_commits SET jsonl_record_count=2 WHERE commit_id=(SELECT MIN(commit_id) FROM storage_commits)",
    "UPDATE item_catalog SET commit_id=commit_id+1 WHERE item_sequence=1",
    "UPDATE item_catalog SET commit_id=999999 WHERE item_sequence=32",
    "UPDATE item_catalog SET commit_id=NULL WHERE item_sequence=32",
])
def test_mixed_commit_audit_rejects_counts_and_wrong_owners_without_reclaiming_tail(
    commit_history_factory, operation, corruption,
):
    storage, session_id, thread_id = commit_history_factory(64)
    jsonl = storage.jsonl_path(session_id, thread_id=thread_id)
    with jsonl.open("ab") as stream:
        stream.write(b"uncommitted-tail")
    before = jsonl.read_bytes()
    with sqlite3.connect(storage.index_path(session_id, thread_id=thread_id)) as connection:
        connection.execute(corruption)
    with pytest.raises(RuntimeError):
        if operation == "initialize":
            storage.initialize(session_id, thread_id=thread_id)
        else:
            with storage.open_read_snapshot(session_id, thread_id):
                pytest.fail("不能接受 commit 数量或 item 归属损坏的历史")
    assert jsonl.read_bytes() == before
