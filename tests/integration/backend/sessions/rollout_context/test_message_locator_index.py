"""真实消息恢复的 locator 索引、既有库初始化和重复坐标拒绝。"""

from __future__ import annotations

import sqlite3

import pytest
from langchain_core.messages import AIMessage

from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_checkpoint_message_groups import _save
from tests.integration.backend.sessions.test_checkpoint_message_groups import (
    group_case as group_case,  # noqa: PLC0414 - 沿当前测试文件隔离真实消息组工作区
)


@pytest.fixture
def locator_case(tmp_path, group_case):
    session, _, messages, _ = group_case
    messages.extend(AIMessage(id=f"locator-{index}", content=f"历史正文 {index}") for index in range(96))
    saver, config = _save(tmp_path, group_case)
    storage = saver._storage.storage
    thread = config["configurable"]["thread_id"]
    with saver:
        yield saver, config, session, thread, storage.index_path(session, thread_id=thread)


@pytest.mark.parametrize("existing", [False, True])
def test_cold_checkpoint_read_preserves_rollout_after_creation_or_initialization(locator_case, existing):
    saver, config, session, thread, path = locator_case
    storage = saver._storage.storage
    canonical = storage.jsonl_path(session, thread_id=thread)
    original_bytes = canonical.read_bytes()
    expected = saver.get_tuple(config)
    with sqlite3.connect(path) as connection:
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='item_catalog_locator_index'",
        ).fetchone()[0]
        assert "UNIQUE" not in index_sql
        if existing:
            connection.execute("DROP INDEX item_catalog_locator_index")
        before = tuple(line for line in connection.iterdump() if line != index_sql + ";")
    if existing:
        # 既有合法库的只读行为不因缺少可选性能索引而改变，也不趁读取补写索引。
        assert saver.get_tuple(config) == expected
        with sqlite3.connect(path) as connection:
            assert tuple(connection.iterdump()) == before
        storage.initialize(session, thread_id=thread)
        storage.initialize(session, thread_id=thread)

    with RolloutCheckpointSaver(storage.sessions_dir) as cold:
        assert cold.get_tuple(config) == expected
    with sqlite3.connect(path) as connection:
        assert tuple(line for line in connection.iterdump() if line != index_sql + ";") == before
    assert canonical.read_bytes() == original_bytes


@pytest.mark.parametrize("damage", ["content_hash", "source_overlay_epoch"])
def test_failed_initialization_does_not_add_locator_index_to_damaged_catalog(locator_case, damage):
    saver, _config, session, thread, path = locator_case
    storage = saver._storage.storage
    canonical = storage.jsonl_path(session, thread_id=thread)
    original_bytes = canonical.read_bytes()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX item_catalog_locator_index")
        if damage == "content_hash":
            connection.execute("UPDATE item_catalog SET content_hash='forged-hash' WHERE item_sequence=1")
        else:
            connection.execute("UPDATE database_meta SET source_overlay_epoch=-1")
        connection.commit()
        before = tuple(connection.iterdump())
    with pytest.raises(RuntimeError, match=damage):
        storage.initialize(session, thread_id=thread)
    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == before
    assert canonical.read_bytes() == original_bytes


def test_locator_index_keeps_duplicate_coordinate_detection(locator_case):
    saver, config, session, thread, path = locator_case
    storage = saver._storage.storage
    canonical = storage.jsonl_path(session, thread_id=thread)
    original_bytes = canonical.read_bytes()
    with sqlite3.connect(path) as connection:
        offset, length = connection.execute(
            "SELECT jsonl_offset,jsonl_length FROM messages ORDER BY message_sequence LIMIT 1",
        ).fetchone()
        connection.execute(
            "UPDATE item_catalog SET jsonl_offset=?,jsonl_length=? WHERE item_id='item-locator-0'",
            (offset, length),
        )
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE jsonl_offset=? AND jsonl_length=?", (offset, length),
        ).fetchone() == (2,)
        before = tuple(connection.iterdump())
    with pytest.raises(RuntimeError, match="message locator 必须恰好指向一个 canonical item"):
        saver.get_tuple(config)
    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == before
    assert canonical.read_bytes() == original_bytes
