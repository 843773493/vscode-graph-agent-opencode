"""只验证版本 locator 的局部查询合同；多版本产品验收另由集成入口覆盖。"""

from __future__ import annotations

import sqlite3

import pytest

from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    canonical_message_anchor,
)
from app.services.infrastructure.rollout_context.storage.catalog.message_groups import (
    find_message_locator,
)


@pytest.fixture
def locator_database():
    with sqlite3.connect(":memory:") as connection:
        # 同 logical ID 的不可变版本用于局部消歧，不冒充完整执行存储。
        connection.executescript("""
            CREATE TABLE messages(message_sequence INTEGER, message_id TEXT, turn_id TEXT,
                execution_id TEXT, jsonl_offset INTEGER, jsonl_length INTEGER);
            CREATE TABLE item_catalog(item_id TEXT, jsonl_offset INTEGER, jsonl_length INTEGER);
            INSERT INTO messages VALUES (1,'logical','turn','execution-old',0,10),
                (2,'logical','turn','execution-new',10,20),(3,'other','turn','execution-other',30,30);
            INSERT INTO item_catalog VALUES ('old-item',0,10),('new-item',10,20),('other-item',30,30);
        """)
        yield connection


def test_logical_id_requires_exact_version_when_multiple_exist(locator_database):
    with pytest.raises(RuntimeError, match="版本歧义"):
        find_message_locator(locator_database, "logical")
    assert find_message_locator(locator_database, "logical", item_id="old-item") == (1, "turn", "execution-old", 0, 10)
    assert find_message_locator(locator_database, "logical", item_id="new-item") == (2, "turn", "execution-new", 10, 20)
    assert find_message_locator(locator_database, "logical", sequences=[1]) == (1, "turn", "execution-old", 0, 10)
    assert find_message_locator(locator_database, "logical", sequences=[2]) == (2, "turn", "execution-new", 10, 20)


def test_explicit_item_never_falls_back_to_another_version(locator_database):
    for item_id in ("missing", "other-item"):
        with pytest.raises(RuntimeError, match="缺少指定 canonical item"):
            find_message_locator(locator_database, "logical", item_id=item_id)
    with pytest.raises(RuntimeError, match="缺少指定 canonical item"):
        find_message_locator(locator_database, "logical", item_id="new-item", sequences=[1])
    assert find_message_locator(locator_database, "logical", sequences=[3]) is None
    assert find_message_locator(locator_database, "not-published") is None


def test_dangling_message_projection_is_an_error(locator_database):
    locator_database.execute("DELETE FROM item_catalog WHERE item_id='other-item'")
    with pytest.raises(RuntimeError, match="缺少 canonical item"):
        find_message_locator(locator_database, "other")
    with pytest.raises(RuntimeError, match="缺少 canonical item"):
        find_message_locator(locator_database, "other", sequences=[3])


def test_canonical_anchor_uses_explicit_group_order():
    assert canonical_message_anchor({"data": {"id": "logical"}}) is None
    assert canonical_message_anchor({"data": {"id": "logical", "response_metadata": {
        "canonical_item_manifests": [{"item_id": "content"}, {"item_id": "exact-anchor"}],
    }}}) == "exact-anchor"


@pytest.mark.parametrize("manifests", [[], {}, [None], [{"item_id": ""}], [{"item_id": "same"}, {"item_id": "same"}]])
def test_invalid_explicit_anchor_is_never_ignored(manifests):
    with pytest.raises(ValueError, match="canonical group manifest"):
        canonical_message_anchor({"data": {"id": "logical", "response_metadata": {
            "canonical_item_manifests": manifests,
        }}})
