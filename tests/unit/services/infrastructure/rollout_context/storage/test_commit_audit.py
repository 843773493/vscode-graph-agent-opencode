"""完整审计沿主键顺序流式读取，限制历史增长带来的排序与正文内存。"""

from __future__ import annotations

import json
import sqlite3
import tracemalloc
from pathlib import Path
from time import perf_counter

import pytest

from app.core.identifier import create_uuid_hex
from app.domain.itemized.records import CanonicalItemRecord
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
    catalog_queries = [statement for statement in statements if "FROM item_catalog item " in statement]
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
