from __future__ import annotations

from app.services.mapping.turn_response_parts import response_parts_from_records

NOW = "2026-09-09T00:00:00+00:00"


def _record(sequence: int, message: dict[str, object]) -> dict[str, object]:
    return {"_indexed_sequence": sequence, "message": message}


def _activity(
    item_id: str,
    item_sequence: int,
    kind: str,
    **extra: object,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "item_sequence": item_sequence,
        "part_ordinal": 0,
        "kind": kind,
        "status": "completed",
        "created_at": NOW,
        "elapsed_ms": 10,
        "message_sequence": 0,
        "text": "",
        **extra,
    }


def _projection(*items: dict[str, object], status: str = "completed") -> dict[str, object]:
    return {
        "status": status,
        "activity_items": list(items),
        "final_message_sequence": 3,
        "final_response_text": "完成",
        "final_response_text_truncated": False,
        "final_item_id": "item-final",
        "final_item_sequence": 8,
        "final_item_created_at": NOW,
    }


def test_detail_keeps_backend_order_and_only_enriches_tool_payload() -> None:
    records = [
        _record(
            5,
            {
                "type": "ai",
                "data": {
                    "content": [],
                    "tool_calls": [
                        {"id": "call-1", "name": "inspect_fixture", "args": {"path": "a"}}
                    ],
                },
            },
        ),
        _record(
            6,
            {
                "type": "tool",
                "data": {"tool_call_id": "call-1", "content": "tool result"},
            },
        ),
        _record(
            3,
            {"type": "ai", "data": {"content": [{"type": "text", "text": "完成"}]}},
        ),
    ]
    projection = _projection(
        _activity("reasoning-1", 2, "reasoning", text="先分析"),
        _activity(
            "tool-call-1",
            3,
            "tool_call",
            tool_call_id="call-1",
            tool_name="inspect_fixture",
            message_sequence=5,
            assistant_message_sequence=5,
            call_index=0,
        ),
        _activity(
            "tool-result-1",
            4,
            "tool_result",
            tool_call_id="call-1",
            tool_name="inspect_fixture",
            message_sequence=6,
            assistant_message_sequence=5,
            result_message_sequence=6,
            call_index=0,
        ),
        _activity("reasoning-2", 7, "reasoning", text="确认结果"),
    )

    parts = response_parts_from_records(
        records,
        projection=projection,
        mode="detail",
        include=frozenset(
            {"reasoning_detail", "tool_call", "tool_result", "final_response"}
        ),
    )

    assert [part.kind for part in parts] == [
        "reasoning",
        "tool_call",
        "tool_result",
        "reasoning",
        "final_text",
    ]
    assert parts[1].arguments == '{"path": "a"}'
    assert parts[2].result == "tool result"
    assert [part.source.item_sequence for part in parts] == [2, 3, 4, 7, 8]


def test_summary_uses_sqlite_projection_without_materializing_records() -> None:
    parts = response_parts_from_records(
        [],
        projection=_projection(
            _activity("summary-1", 2, "reasoning_summary", text="摘要"),
            _activity(
                "tool-call-1",
                3,
                "tool_call",
                tool_call_id="call-1",
                tool_name="inspect_fixture",
            ),
        ),
        mode="summary",
        include=frozenset({"reasoning_summary", "tool_summary", "final_response"}),
    )

    assert [part.kind for part in parts] == [
        "reasoning_summary",
        "tool_call",
        "final_text",
    ]
    assert all(part.projection == "summary" for part in parts)
    assert parts[0].part_id == "summary-1:part:0"


def test_identical_reasoning_text_with_distinct_identity_is_preserved() -> None:
    parts = response_parts_from_records(
        [],
        projection=_projection(
            _activity("reasoning-1", 2, "reasoning", text="相同正文"),
            _activity("reasoning-2", 7, "reasoning", text="相同正文"),
        ),
        mode="detail",
        include=frozenset({"reasoning_detail"}),
    )

    assert [part.part_id for part in parts] == [
        "reasoning-1:part:0",
        "reasoning-2:part:0",
    ]


def test_partial_final_text_keeps_interrupt_semantics() -> None:
    records = [
        _record(
            3,
            {
                "type": "ai",
                "data": {
                    "content": [{"type": "text", "text": "完成"}],
                    "response_metadata": {
                        "completion_reason": "user_interrupt",
                        "partial": True,
                    },
                },
            },
        )
    ]

    for mode in ("summary", "detail"):
        parts = response_parts_from_records(
            records,
            projection=_projection(status="cancelled"),
            mode=mode,
            include=frozenset({"final_response"}),
        )
        assert len(parts) == 1
        assert parts[0].kind == "text"
        assert parts[0].final is False
        assert parts[0].completion_reason == "user_interrupt"


def test_terminal_tool_call_without_result_is_explicitly_unknown() -> None:
    parts = response_parts_from_records(
        [],
        projection=_projection(
            _activity(
                "tool-call-1",
                3,
                "tool_call",
                tool_call_id="call-unknown",
                tool_name="read_file",
            )
        ),
        mode="detail",
        include=frozenset({"tool_call", "tool_result"}),
    )

    assert len(parts) == 1
    assert parts[0].status == "failed"
    assert parts[0].outcome_unknown is True
