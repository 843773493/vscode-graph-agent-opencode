from __future__ import annotations

import json

import pytest

from app.core import bounded_json
from app.core.bounded_json import bound_json_value


def test_bound_json_value_preserves_shape_and_marks_large_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = {"result": "x" * 1024 * 1024, "status": "completed"}
    source_text = value["result"]
    original_encoded = bounded_json._encoded
    encoded_values: list[object] = []

    def record_encoded_value(item: object) -> bytes:
        encoded_values.append(item)
        return original_encoded(item)

    monkeypatch.setattr(bounded_json, "_encoded", record_encoded_value)
    bounded = bound_json_value(value, max_bytes=4096)

    assert isinstance(bounded, dict)
    assert bounded["status"] == "completed"
    assert "BoxTeam 已截断" in bounded["result"]
    assert all(item is not source_text for item in encoded_values)
    assert (
        len(
            json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        <= 4096
    )


def test_bound_json_value_skips_serializing_oversized_top_level_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = "x" * (1024 * 1024)
    original_encoded = bounded_json._encoded
    encoded_values: list[object] = []

    def record_encoded_value(item: object) -> bytes:
        encoded_values.append(item)
        return original_encoded(item)

    monkeypatch.setattr(bounded_json, "_encoded", record_encoded_value)

    bounded = bound_json_value(source_text, max_bytes=4096)

    assert isinstance(bounded, str)
    assert "BoxTeam 已截断" in bounded
    assert all(item is not source_text for item in encoded_values)
    assert len(original_encoded(bounded)) <= 4096


def test_bound_json_value_marks_large_structures_without_invalid_json() -> None:
    value = [{"index": index, "text": "x" * 1024} for index in range(100)]

    bounded = bound_json_value(value, max_bytes=2048)

    assert (
        len(
            json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        <= 2048
    )


def test_bound_json_value_keeps_mapping_prefix_and_avoids_reencoding_it(
    monkeypatch,
) -> None:
    item_count = 512
    value = {f"item-{index:04d}": "x" * 40 for index in range(item_count)}
    original_encoded = bounded_json._encoded(value)
    serialized_bytes = 0
    original_encode = bounded_json._encoded

    def count_serialized_bytes(item: object) -> bytes:
        nonlocal serialized_bytes
        encoded = original_encode(item)
        serialized_bytes += len(encoded)
        return encoded

    monkeypatch.setattr(bounded_json, "_encoded", count_serialized_bytes)

    compacted = bound_json_value(value, max_bytes=4101)

    assert isinstance(compacted, dict)
    retained_items = [key for key in compacted if key != "__boxteam_truncated__"]
    assert retained_items
    assert retained_items == [
        f"item-{index:04d}" for index in range(len(retained_items))
    ]
    assert "item-0511" not in compacted
    assert compacted["__boxteam_truncated__"] is True
    assert (
        len(
            json.dumps(compacted, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        <= 4101
    )
    assert serialized_bytes < len(original_encoded) * 4


def test_bound_json_value_skips_compacting_discarded_list_tail(monkeypatch) -> None:
    source_items = [{"first": "x" * 40, "second": "y" * 40} for _ in range(128)]
    source_item_ids = {id(item) for item in source_items}
    original_compact = bounded_json._compact
    compacted_source_item_count = 0

    def count_source_items(
        item: object,
        max_bytes: int,
        *,
        serialized_source: bytes | None = None,
    ) -> tuple[object, int]:
        nonlocal compacted_source_item_count
        if id(item) in source_item_ids:
            compacted_source_item_count += 1
        return original_compact(item, max_bytes, serialized_source=serialized_source)

    monkeypatch.setattr(bounded_json, "_compact", count_source_items)

    compacted = bound_json_value(source_items, max_bytes=1040)

    assert isinstance(compacted, list)
    assert compacted[-1] == "...[BoxTeam list items 已截断]"
    assert 0 < compacted_source_item_count < len(source_items) // 2
    assert len(bounded_json._encoded(compacted)) <= 1040


def test_bound_json_value_drops_oversized_scalar_before_marking_mapping() -> None:
    value = {"large_number": 10**1000, "tail": "x"}

    bounded = bound_json_value(value, max_bytes=512)

    assert bounded == {"__boxteam_truncated__": True}


def test_bound_json_value_honours_budget_at_lower_bound() -> None:
    """在文档允许的最小预算附近，序列化结果也不得超出上限。

    ``_truncated_text`` 只按原始文本字节裁剪，未计入 JSON 字符串的两枚引号
    与转义开销；超长字符串的截断结果因此会超出预算，进而落到固定 153 字节
    的兜底标记上。预算取 128（构造函数允许的最小值）时该兜底标记本身就
    超限，函数承诺的「序列化后不超过上限」被破坏。
    """
    for max_bytes in (128, 130, 140, 152):
        bounded = bound_json_value("x" * 100_000, max_bytes=max_bytes)
        encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        assert len(encoded) <= max_bytes, (max_bytes, len(encoded), bounded)
        assert "截断" in str(bounded)
