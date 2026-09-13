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


def test_detail_backfills_scoped_canonical_tool_call_arguments_by_raw_id() -> None:
    records = [
        _record(
            10,
            {
                "type": "ai",
                "data": {
                    "content": [],
                    "tool_calls": [
                        {
                            "id": "call-pwd",
                            "name": "exec_command",
                            "args": {
                                "cmd": "pwd",
                                "login": True,
                                "yield_time_ms": 10000,
                            },
                        }
                    ],
                },
            },
        )
    ]
    parts = response_parts_from_records(
        records,
        projection=_projection(
            _activity(
                "canonical-tool-call",
                21,
                "tool_call",
                tool_call_id="model-call:tool-call:call-pwd",
                tool_name="exec_command",
            )
        ),
        mode="detail",
        include=frozenset({"tool_call"}),
    )

    assert parts[0].arguments == (
        '{"cmd": "pwd", "login": true, "yield_time_ms": 10000}'
    )


def test_detail_does_not_guess_arguments_when_raw_tool_call_id_is_ambiguous() -> None:
    records = [
        _record(
            10,
            {
                "type": "ai",
                "data": {
                    "content": [],
                    "tool_calls": [
                        {"id": "call-reused", "name": "first", "args": {"n": 1}}
                    ],
                },
            },
        ),
        _record(
            11,
            {
                "type": "ai",
                "data": {
                    "content": [],
                    "tool_calls": [
                        {"id": "call-reused", "name": "second", "args": {"n": 2}}
                    ],
                },
            },
        ),
    ]
    parts = response_parts_from_records(
        records,
        projection=_projection(
            _activity(
                "canonical-tool-call",
                21,
                "tool_call",
                tool_call_id="model-call:tool-call:call-reused",
                tool_name="second",
            )
        ),
        mode="detail",
        include=frozenset({"tool_call"}),
    )

    assert parts[0].arguments is None


def test_tool_parts_stay_summary_when_detail_mode_lacks_tool_payload() -> None:
    """include 含 thinking 却没有 tool_call 时，工具部件不得标成 detail。

    默认 initial include 为 ``["user","thinking","tool_summary","final_response"]``：
    ``thinking`` 会把整个请求判定为 detail 模式，但该请求并没有加载工具参数。
    若工具部件仍标记为 detail，前端 ``detailsLoaded`` 会误判为已加载，展开时
    不再补拉参数，最终只显示“输入参数”标题却没有正文。
    """
    parts = response_parts_from_records(
        [],
        projection=_projection(
            _activity("reasoning-1", 2, "reasoning", text="分析"),
            _activity(
                "tool-call-1",
                3,
                "tool_call",
                tool_call_id="call-1",
                tool_name="exec_command",
            ),
            _activity(
                "tool-result-1",
                4,
                "tool_result",
                tool_call_id="call-1",
                tool_name="exec_command",
            ),
        ),
        mode="detail",
        include=frozenset(
            {"user", "thinking", "tool_summary", "final_response"}
        ),
    )

    tool_parts = [part for part in parts if part.kind in {"tool_call", "tool_result"}]
    assert tool_parts, "tool_summary 请求必须保留工具占位部件"
    assert [part.projection for part in tool_parts] == ["summary", "summary"]
    assert all(part.arguments is None for part in tool_parts)


def test_tool_parts_are_detail_when_tool_payload_is_included() -> None:
    """显式请求 tool_call/tool_result 时，工具部件才标记为 detail。"""
    parts = response_parts_from_records(
        [],
        projection=_projection(
            _activity(
                "tool-call-1",
                3,
                "tool_call",
                tool_call_id="call-1",
                tool_name="exec_command",
            ),
            _activity(
                "tool-result-1",
                4,
                "tool_result",
                tool_call_id="call-1",
                tool_name="exec_command",
            ),
        ),
        mode="detail",
        include=frozenset({"tool_call", "tool_result"}),
    )

    tool_parts = [part for part in parts if part.kind in {"tool_call", "tool_result"}]
    assert [part.projection for part in tool_parts] == ["detail", "detail"]
