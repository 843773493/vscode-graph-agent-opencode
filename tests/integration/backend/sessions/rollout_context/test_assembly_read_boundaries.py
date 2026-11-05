"""真实封存内容的同批提交复用，下一次操作和逐 item 损坏检测仍独立。"""

import json
import sqlite3
from collections import Counter
from time import perf_counter

import pytest

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.assembly.content_store import (
    store_snapshot,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.catalog import item_reader
from tests.integration.backend.sessions.test_checkpoint_message_groups import _save
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    group_case as group_case,  # noqa: PLC0414 - 真实消息组按当前测试文件隔离工作区
)


@pytest.fixture(params=["cold_read", "encode"])
def assembly_batch(tmp_path, group_case, frozen_provider_prepare, request):
    session, _, messages, _ = group_case
    # 一个工具消息的长 content parts 由真实 checkpoint 写入同一 item commit。
    messages[1].content.extend(
        {"type": "text", "text": f"assembly-boundary:{index}:" + "历史正文。" * 64}
        for index in range(32)
    )
    saver, config = _save(tmp_path, group_case)
    original = frozen_provider_prepare(saver, key="assembly-boundary-create")
    thread = config["configurable"]["thread_id"]
    storage = saver._storage.storage
    path = storage.index_path(session, thread_id=thread)
    snapshot_value = json.loads(canonical_json_bytes(original.to_dict()))
    with sqlite3.connect(path) as connection:
        encoded = connection.execute(
            "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (original.assembly_id,),
        ).fetchone()[0]
        captured = {node["item_id"] for row in connection.execute("SELECT body_json FROM assembly_content_nodes")
                    if (node := json.loads(row[0]))["kind"] == "canonical"}
        assert len(captured) == 32
        commits = {row[0] for item in captured for row in connection.execute(
            "SELECT commit_id FROM item_catalog WHERE item_id=?", (item,),
        )}
    assert len(commits) == 1

    def read():
        if request.param == "cold_read":
            with RolloutCheckpointSaver(storage.sessions_dir) as cold:
                return cold.get_context_assembly(session, thread, assembly_id=original.assembly_id).to_dict()
        with sqlite3.connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = store_snapshot(connection, snapshot_value)
            connection.rollback()
            return result

    expected = original.to_dict() if request.param == "cold_read" else encoded
    return read, expected, path, commits, sorted(captured)[17], storage.jsonl_path(session, thread_id=thread)


@pytest.mark.parametrize("damage", ["commit_metadata", "item_locator", "item_projection"])
def test_assembly_content_reuses_boundaries_but_rechecks_each_operation(assembly_batch, monkeypatch, damage):
    read, expected, path, commits, member, jsonl = assembly_batch
    before = jsonl.read_bytes()
    checked_commits, checked_scopes = [], []
    validate_commit = item_reader.validate_stored_commit
    validate_scope = item_reader.require_historical_scope

    def commit(row, **kwargs):
        checked_commits.append(row[0])
        return validate_commit(row, **kwargs)

    def scope(connection, session, thread, **kwargs):
        checked_scopes.append((session, thread))
        return validate_scope(connection, session, thread, **kwargs)

    monkeypatch.setattr(item_reader, "validate_stored_commit", commit)
    monkeypatch.setattr(item_reader, "require_historical_scope", scope)
    for _ in range(2):
        checked_commits.clear()
        checked_scopes.clear()
        started = perf_counter()
        assert read() == expected
        print(json.dumps({"milliseconds": (perf_counter() - started) * 1000,
                          "commit_checks": len(checked_commits), "scope_checks": len(checked_scopes)}))
        assert Counter(checked_commits) == Counter({value: 1 for value in commits})
        assert len(checked_scopes) == 1

    with sqlite3.connect(path) as connection:
        if damage == "commit_metadata":
            # 当前末尾提交未改；重新消费旧 commit 时仍须发现规范字节损坏。
            connection.execute("UPDATE storage_commits SET metadata_json=metadata_json||' ' WHERE commit_id=?", (min(commits),))
            error = "metadata_json 不是 RFC 8785 JCS object"
        elif damage == "item_locator":
            connection.execute("UPDATE item_catalog SET jsonl_length=jsonl_length+1 WHERE item_id=?", (member,))
            error = "JSONL|offset"
        else:
            connection.execute("UPDATE item_catalog SET tool_call_id='forged' WHERE item_id=?", (member,))
            error = "tool_call_id 不一致"
    with pytest.raises(RuntimeError, match=error):
        read()
    assert jsonl.read_bytes() == before
