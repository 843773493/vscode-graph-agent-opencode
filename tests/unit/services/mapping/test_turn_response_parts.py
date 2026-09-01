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
            "status": "completed",
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

    tool_part = next(part for part in parts if part.kind == "tool_call")
    assert tool_part.status == "failed"
    assert tool_part.outcome_unknown is True


def test_partial_final_text_keeps_interrupt_semantics_in_summary_and_detail() -> None:
    records = [
        _record(
            2,
            {
                "type": "ai",
                "data": {
                    "content": [{"type": "text", "text": "半截回答"}],
                    "response_metadata": {
                        "completion_reason": "user_interrupt",
                        "partial": True,
                    },
                },
            },
        )
    ]
    projection = {
        "status": "cancelled",
        "final_message_sequence": 2,
        "final_response_text": "半截回答",
    }

    summary_parts = response_parts_from_records(
        records,
        projection=projection,
        mode="summary",
        include=frozenset({"final_response"}),
    )
    detail_parts = response_parts_from_records(
        records,
        projection=projection,
        mode="detail",
        include=frozenset({"final_response", "text"}),
    )

    for parts in (summary_parts, detail_parts):
        assert len(parts) == 1
        assert parts[0].kind == "text"
        assert parts[0].final is False
        assert parts[0].partial is True
        assert parts[0].completion_reason == "user_interrupt"


def test_detail_with_tool_summary_emits_payload_free_tool_parts() -> None:
    parts = response_parts_from_records(
        [
            _record(
                1,
                {
                    "type": "ai",
                    "data": {
                        "content": [{"type": "text", "text": "准备检查"}],
                        "tool_calls": [],
                    },
                },
            )
        ],
        projection={
            "status": "completed",
            "final_message_sequence": 1,
            "tool_items": [
                {
                    "item_kind": "tool_call",
                    "sequence": 2,
                    "assistant_message_sequence": 2,
                    "call_index": 0,
                    "tool_call_id": "call-1",
                    "tool_name": "inspect_fixture",
                    "status": "success",
                },
                {
                    "item_kind": "tool_result",
                    "sequence": 3,
                    "assistant_message_sequence": 2,
                    "call_index": 0,
                    "tool_call_id": "call-1",
                    "tool_name": "inspect_fixture",
                    "status": "success",
                },
            ],
        },
        mode="detail",
        include=frozenset({"text", "tool_summary", "final_response"}),
    )

    assert [part.kind for part in parts] == ["final_text", "tool_call"]
    assert parts[1].projection == "summary"
    assert parts[1].status == "completed"
    assert parts[1].outcome_unknown is False
    assert parts[1].arguments is None
    assert parts[1].result is None


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


def test_detail_terminal_turn_without_tool_result_is_outcome_unknown() -> None:
    parts = response_parts_from_records(
        [
            _record(
                7,
                {
                    "type": "ai",
                    "data": {
                        "content": [],
                        "tool_calls": [
                            {
                                "id": "call-unknown",
                                "name": "read_file",
                                "args": {"path": "README.md"},
                            }
                        ],
                    },
                },
            )
        ],
        projection={"status": "completed", "final_message_sequence": 7},
        mode="detail",
        include=frozenset({"tool_call", "tool_result"}),
    )

    assert len(parts) == 1
    assert parts[0].status == "failed"
    assert parts[0].outcome_unknown is True


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
