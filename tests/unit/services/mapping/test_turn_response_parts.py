from __future__ import annotations

from app.services.mapping.turn_response_parts import response_parts_from_records


def _record(sequence: int, message: dict[str, object]) -> dict[str, object]:
    return {"_indexed_sequence": sequence, "message": message}


def test_detail_keeps_content_then_tool_call_then_tool_result_order() -> None:
    records = [
        _record(
            1,
            {
                "type": "ai",
                "data": {
                    "content": [
                        {"type": "reasoning_content", "reasoning_content": "先分析"},
                        {"type": "text", "text": "准备调用工具"},
                    ],
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "inspect_fixture",
                            "args": {"path": "a"},
                        },
                    ],
                },
            },
        ),
        _record(
            2,
            {
                "type": "tool",
                "data": {
                    "tool_call_id": "call-1",
                    "content": "tool result",
                    "status": "success",
                },
            },
        ),
        _record(
            3,
            {
                "type": "ai",
                "data": {
                    "content": [{"type": "text", "text": "完成"}],
                    "tool_calls": [],
                },
            },
        ),
    ]

    parts = response_parts_from_records(
        records,
        projection={"final_message_sequence": 3},
        mode="detail",
        include=frozenset(
            {"text", "reasoning_detail", "tool_call", "tool_result", "final_response"}
        ),
    )

    assert [part.kind for part in parts] == [
        "reasoning",
        "text",
        "tool_call",
        "tool_result",
        "final_text",
    ]
    assert parts[2].source.call_index == 0
    assert parts[3].source.result_message_sequence == 2
    assert parts[-1].final is True


def test_summary_uses_projection_without_materializing_records() -> None:
    parts = response_parts_from_records(
        [],
        projection={
            "final_message_sequence": 3,
            "final_response_text": "完成",
            "thinking_blocks": [
                {
                    "kind": "summary",
                    "text": "摘要",
                    "message_sequence": 1,
                    "content_block_index": 0,
                    "item_index": 0,
                    "carrier_type": "reasoning_items",
                }
            ],
            "tool_items": [
                {
                    "item_kind": "tool_call",
                    "sequence": 1,
                    "call_index": 0,
                    "tool_call_id": "call-1",
                    "tool_name": "inspect_fixture",
                    "status": "succeeded",
                }
            ],
        },
        mode="summary",
        include=frozenset({"reasoning_summary", "tool_summary", "final_response"}),
    )

    assert [part.kind for part in parts] == [
        "reasoning_summary",
        "tool_call",
        "final_text",
    ]
    assert all(part.projection == "summary" for part in parts)


def test_detail_include_tool_result_does_not_invent_tool_call() -> None:
    parts = response_parts_from_records(
        [
            _record(
                1,
                {
                    "type": "ai",
                    "data": {
                        "content": [{"type": "text", "text": "完成"}],
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "name": "inspect_fixture",
                                "args": {"path": "a"},
                            }
                        ],
                    },
                },
            ),
            _record(
                2,
                {
                    "type": "tool",
                    "data": {
                        "tool_call_id": "call-1",
                        "content": "结果",
                    },
                },
            ),
        ],
        projection={"final_message_sequence": 1},
        mode="detail",
        include=frozenset({"tool_result"}),
    )

    assert [part.kind for part in parts] == ["tool_result"]


def test_duplicate_tool_call_ids_use_assistant_source_coordinates() -> None:
    parts = response_parts_from_records(
        [
            _record(
                1,
                {
                    "type": "ai",
                    "data": {
                        "content": [],
                        "tool_calls": [
                            {"id": "reused", "name": "first", "args": {}}
                        ],
                    },
                },
            ),
            _record(
                2,
                {
                    "type": "tool",
                    "data": {"tool_call_id": "reused", "content": "first result"},
                },
            ),
            _record(
                3,
                {
                    "type": "ai",
                    "data": {
                        "content": [],
                        "tool_calls": [
                            {"id": "reused", "name": "second", "args": {}}
                        ],
                    },
                },
            ),
            _record(
                4,
                {
                    "type": "tool",
                    "data": {"tool_call_id": "reused", "content": "second result"},
                },
            ),
        ],
        projection=None,
        mode="detail",
        include=frozenset({"tool_call", "tool_result"}),
    )

    assert [part.part_id for part in parts] == [
        "tool-call:1:0",
        "tool-call:1:0",
        "tool-call:3:0",
        "tool-call:3:0",
    ]
    assert [part.source.assistant_message_sequence for part in parts] == [
        1,
        1,
        3,
        3,
    ]
