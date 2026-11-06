"""只验证版本 locator 的局部查询合同；多版本产品验收另由集成入口覆盖。"""

from __future__ import annotations

import sqlite3

import pytest
from langchain_core.messages import AIMessage, BaseMessage, message_to_dict

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
            CREATE TABLE messages(message_sequence INTEGER PRIMARY KEY, message_id TEXT, turn_id TEXT,
                execution_id TEXT, jsonl_offset INTEGER, jsonl_length INTEGER);
            CREATE TABLE item_catalog(item_id TEXT UNIQUE, jsonl_offset INTEGER, jsonl_length INTEGER,
                turn_id TEXT, execution_id TEXT);
            CREATE INDEX messages_logical_identity ON messages(message_id,message_sequence);
            INSERT INTO messages VALUES (1,'logical','turn','execution-old',0,10),
                (2,'logical','turn','execution-new',10,20),(3,'other','turn','execution-other',30,30);
            INSERT INTO item_catalog VALUES ('old-item',0,10,'turn','execution-old'),
                ('new-item',10,20,'turn','execution-new'),('other-item',30,30,'turn','execution-other');
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


def test_carrier_anchor_reads_metadata_without_serializing_or_caching_body(monkeypatch):
    carrier = AIMessage(content="正文" * 4096, id="logical", response_metadata={
        "canonical_item_manifests": [{"item_id": "content"}, {"item_id": "old-anchor"}],
    })
    assert canonical_message_anchor(message_to_dict(carrier)) == "old-anchor"

    def refuse_dump(*args, **kwargs):
        raise AssertionError("读取 anchor 不得序列化正文")

    monkeypatch.setattr(BaseMessage, "model_dump", refuse_dump)
    assert canonical_message_anchor(carrier) == "old-anchor"
    carrier.response_metadata["canonical_item_manifests"][-1]["item_id"] = "new-anchor"
    assert canonical_message_anchor(carrier) == "new-anchor"
    carrier.response_metadata.clear()
    assert canonical_message_anchor(carrier) is None


@pytest.mark.parametrize("manifests", [[], {}, [None], [{"item_id": ""}], [{"item_id": "same"}, {"item_id": "same"}]])
def test_carrier_anchor_keeps_serialized_manifest_validation(manifests):
    carrier = AIMessage(content="不应影响身份校验", id="logical", response_metadata={
        "canonical_item_manifests": manifests,
    })
    for value in (carrier, message_to_dict(carrier)):
        with pytest.raises(ValueError, match="canonical group manifest"):
            canonical_message_anchor(value)


@pytest.mark.parametrize("manifests", [[], {}, [None], [{"item_id": ""}], [{"item_id": "same"}, {"item_id": "same"}]])
def test_invalid_explicit_anchor_is_never_ignored(manifests):
    with pytest.raises(ValueError, match="canonical group manifest"):
        canonical_message_anchor({"data": {"id": "logical", "response_metadata": {
            "canonical_item_manifests": manifests,
        }}})


@pytest.mark.parametrize("sequences", [[], [3], [1, 1]])
def test_view_filter_keeps_empty_and_repeated_selections_exact(locator_database, sequences):
    expected = (1, "turn", "execution-old", 0, 10) if 1 in sequences else None
    assert find_message_locator(locator_database, "logical", sequences=sequences) == expected
    if expected is None:
        with pytest.raises(RuntimeError, match="缺少指定 canonical item"):
            find_message_locator(locator_database, "logical", item_id="old-item", sequences=sequences)


def test_view_filter_excludes_dangling_unselected_versions(locator_database):
    locator_database.execute("DELETE FROM item_catalog WHERE item_id='new-item'")
    assert find_message_locator(locator_database, "logical", sequences=[1]) == (1, "turn", "execution-old", 0, 10)
    with pytest.raises(RuntimeError, match="缺少 canonical item"):
        find_message_locator(locator_database, "logical", sequences=[2])
    with pytest.raises(RuntimeError, match="版本歧义"):
        find_message_locator(locator_database, "logical")


def test_selected_message_version_does_not_join_every_historical_version(locator_database):
    # 只造 locator 局部查询数据，不冒充 canonical 正文或执行历史。
    locator_database.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        ((index, "logical", "turn", f"execution-{index}", index * 100, 10) for index in range(4, 1028)),
    )
    locator_database.executemany(
        "INSERT INTO item_catalog VALUES (?,?,?,?,?)",
        ((f"item-{index}", index * 100, 10, "turn", f"execution-{index}") for index in range(4, 1028)),
    )
    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 1
        return 0

    locator_database.set_progress_handler(progress, 1)
    try:
        assert find_message_locator(locator_database, "logical", sequences=[1]) == (1, "turn", "execution-old", 0, 10)
    finally:
        locator_database.set_progress_handler(None, 0)
    # 缺物理 locator 索引时单次连接仍扫描 catalog；不得为1026个版本重复扫描。
    assert instructions < 20_000, instructions
