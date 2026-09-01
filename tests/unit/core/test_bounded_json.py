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
