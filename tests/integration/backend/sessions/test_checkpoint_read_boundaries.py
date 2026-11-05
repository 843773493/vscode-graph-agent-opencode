"""checkpoint/history 成功批读后仍拒绝已提交数据损坏，且不改写 JSONL。"""

import sqlite3

import pytest

from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    _save,
)
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    group_case as group_case,  # noqa: PLC0414 - 复用真实消息组，在当前文件隔离工作区
)


@pytest.fixture(params=["checkpoint", "indexed_history"])
def checkpoint_batch(tmp_path, group_case, request):
    session, _, messages, _ = group_case
    # 保留一个非末尾的 tool_call，覆盖消息组中间成员的损坏。
    messages[1].tool_calls.append(
        {"id": "pending-call", "name": "read", "args": {"path": "pending.txt"}, "type": "tool_call"}
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
        member = connection.execute(
            "SELECT item_id FROM item_catalog WHERE semantic_kind='tool_call' "
            "AND json_extract(projection_manifest_json,'$.projection_group.ordinal') "
            "< json_extract(projection_manifest_json,'$.projection_group.size')-1 "
            "ORDER BY item_sequence LIMIT 1"
        ).fetchone()[0]
    assert len(commits) == 2
    expected = read()
    jsonl = storage.jsonl_path(session, thread_id=thread)
    return read, expected, path, commits, member, jsonl


@pytest.mark.parametrize("damage", ["commit_metadata", "item_locator", "item_projection"])
def test_message_batches_reject_damage_after_success(checkpoint_batch, damage):
    read, expected, path, commits, member, jsonl = checkpoint_batch
    before = jsonl.read_bytes()
    assert read() == expected

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
