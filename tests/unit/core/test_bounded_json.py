from __future__ import annotations

import json

from app.core.bounded_json import bound_json_value


def test_bound_json_value_preserves_shape_and_marks_large_strings() -> None:
    value = {"result": "x" * 1024 * 1024, "status": "completed"}

    bounded = bound_json_value(value, max_bytes=4096)

    assert isinstance(bounded, dict)
    assert bounded["status"] == "completed"
    assert "BoxTeam 已截断" in bounded["result"]
    assert (
        len(
            json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        <= 4096
    )


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


def test_bound_json_value_honours_budget_at_lower_bound() -> None:
    """在文档允许的最小预算附近，序列化结果也不得超出上限。

    ``_truncated_text`` 只按原始文本字节裁剪，未计入 JSON 字符串的两枚引号
    与转义开销；超长字符串的截断结果因此会超出预算，进而落到固定 153 字节
    的兜底标记上。预算取 128（构造函数允许的最小值）时该兜底标记本身就
    超限，函数承诺的「序列化后不超过上限」被破坏。
    """
    for max_bytes in (128, 130, 140, 152):
        bounded = bound_json_value("x" * 100_000, max_bytes=max_bytes)
        encoded = json.dumps(
            bounded, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        assert len(encoded) <= max_bytes, (max_bytes, len(encoded), bounded)
        assert "截断" in str(bounded)
