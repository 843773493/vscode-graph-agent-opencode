"""同次 checkpoint/history 批读复用边界，逐 item 校验和下次损坏检测不省略。"""

import sqlite3
from collections import Counter

import pytest

from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.storage.catalog import item_reader
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    _save,
)
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    group_case as group_case,  # noqa: PLC0414 - 复用真实消息组，在当前文件隔离工作区
)


@pytest.fixture(params=["checkpoint", "indexed_history"])
def checkpoint_batch(tmp_path, group_case, request):
    session, _, messages, _ = group_case
    # 一条真实消息展开为许多 canonical item，必须覆盖同组内及跨组的提交复用。
    messages[1].tool_calls.extend(
        {"id": f"pending-call-{index}", "name": "read", "args": {"path": f"{index}.txt"}, "type": "tool_call"}
        for index in range(32)
    )
    saver, config = _save(tmp_path, group_case)
    thread = config["configurable"]["thread_id"]
    storage = saver._storage.storage
    reader = RolloutContextReader(saver._storage)

    def read():
        if request.param == "checkpoint":
            return saver.get_tuple(config).checkpoint
        with reader.open_snapshot(session, thread) as snapshot:
            return reader.read_projection_records_batch(
                snapshot, chain=reader.resolve_chain(snapshot), turn_ids=("turn-tool-result",),
            )

    path = storage.index_path(session, thread_id=thread)
    with sqlite3.connect(path) as connection:
        commits = {row[0] for row in connection.execute("SELECT DISTINCT commit_id FROM item_catalog")}
        count = connection.execute("SELECT count(*) FROM item_catalog").fetchone()[0]
        member = connection.execute(
            "SELECT item_id FROM item_catalog WHERE semantic_kind='tool_call' "
            "AND json_extract(projection_manifest_json,'$.projection_group.ordinal') "
            "< json_extract(projection_manifest_json,'$.projection_group.size')-1 "
            "ORDER BY item_sequence LIMIT 1"
        ).fetchone()[0]
    assert count > 32 and len(commits) == 2
    expected = read()
    jsonl = storage.jsonl_path(session, thread_id=thread)
    return read, expected, path, commits, member, jsonl


@pytest.mark.parametrize("damage", ["commit_metadata", "item_locator", "item_projection"])
def test_message_batches_reuse_only_commit_and_scope_checks(checkpoint_batch, monkeypatch, damage):
    read, expected, path, commits, member, jsonl = checkpoint_batch
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
        assert read() == expected
        assert Counter(checked_commits) == Counter({value: 1 for value in commits})
        assert len(checked_scopes) == 1

    with sqlite3.connect(path) as connection:
        if damage == "commit_metadata":
            # 损坏旧提交；当前末尾提交仍合法，下一次读取必须重新发现它。
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
