from __future__ import annotations

import hashlib
import tracemalloc
from dataclasses import replace
from datetime import datetime

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers.message_content_schema import validate_content_blocks
from app.agents.providers.response_normalization import canonicalize_ai_message
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.provider_history import reasoning_projection_rows
from app.services.mapping.turn_response_parts import (
    response_parts_from_internal_item,
    response_parts_from_records,
)

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
        "logical_item_ordinal": item_sequence,
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
        "final_logical_item_ordinal": 20,
        "final_item_created_at": NOW,
    }


def _canonical_item(
    *,
    item_id: str,
    item_sequence: int,
    semantic_kind: str,
    payload_kind: str,
    payload: object,
    projection_identity: ItemProjectionIdentity | None = None,
) -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=item_sequence,
        item_id=item_id,
        semantic_kind=semantic_kind,
        payload_kind=payload_kind,
        status="completed",
        producer_ref={"producer_kind": "provider", "producer_id": "execution_1"},
        payload=payload,
        created_at=NOW,
        projection_identity=projection_identity,
    )


def test_internal_execution_response_item_uses_canonical_identity_and_include() -> None:
    item = _canonical_item(
        item_id="item-internal-output",
        item_sequence=17,
        semantic_kind="assistant_output",
        payload_kind="text",
        payload="内部执行回复",
    )

    parts = response_parts_from_internal_item(
        item,
        include=frozenset({"final_response", "internal"}),
        logical_item_ordinal=7,
        elapsed_ms=12,
    )
    excluded = response_parts_from_internal_item(
        item,
        include=frozenset({"internal"}),
        logical_item_ordinal=7,
        elapsed_ms=12,
    )

    assert len(parts) == 1
    assert parts[0].kind == "text"
    assert parts[0].projection == "summary"
    assert parts[0].part_id == "item-internal-output:part:0"
    assert parts[0].source.item_sequence == 17
    assert parts[0].source.logical_item_ordinal == 7
    assert parts[0].source.elapsed_ms == 12
    assert parts[0].source.created_at == datetime.fromisoformat(NOW)
    assert parts[0].text == "内部执行回复"
    assert excluded == []


def test_internal_execution_tool_payload_is_only_detailed_when_requested() -> None:
    item = _canonical_item(
        item_id="item-internal-tool",
        item_sequence=18,
        semantic_kind="tool_call",
        payload_kind="tool_call",
        payload={
            "tool_calls": [
                {"id": "call-1", "name": "read_file", "args": {"path": "a.py"}}
            ]
        },
    )

    summary = response_parts_from_internal_item(
        item,
        include=frozenset({"tool_summary"}),
        logical_item_ordinal=7,
        elapsed_ms=21,
    )
    detail = response_parts_from_internal_item(
        item,
        include=frozenset({"tool_call"}),
        logical_item_ordinal=7,
        elapsed_ms=21,
    )

    assert summary[0].projection == "summary"
    assert summary[0].arguments is None
    assert detail[0].projection == "detail"
    assert detail[0].arguments == '{"path":"a.py"}'


@pytest.fixture
def internal_structured_content() -> CanonicalItemRecord:
    return replace(_canonical_item(
        item_id="item-internal-final-content", item_sequence=19,
        semantic_kind="assistant_output", payload_kind="structured_content",
        projection_identity=ItemProjectionIdentity(
            model_call_id="internal-model", projection_message_id="internal-final",
            phase="final_answer", completion_reason="length",
        ),
        payload=[
            {"type": "text", "text": "开场"},
            {"type": "reasoning_items", "reasoning_items": [
                {"id": "reasoning-readable", "type": "reasoning",
                 "content": [{"type": "reasoning_text", "text": "分析正文"}],
                 "summary": [{"type": "summary_text", "text": "分析摘要"}],
                 "encrypted_content": "不可泄露的密文"},
                {"id": "reasoning-opaque", "type": "reasoning", "encrypted_content": "另一段密文"},
            ]},
            {"type": "refusal", "refusal": "无法提供"},
            {"type": "output_text", "text": "长" * 65537},
        ],
    ), status="partial")


def test_internal_structured_content_preserves_semantics_coordinates_and_bounds(
    internal_structured_content: CanonicalItemRecord,
) -> None:
    parts = response_parts_from_internal_item(
        internal_structured_content,
        include=frozenset({"text", "reasoning_detail", "encrypted_reasoning_meta"}),
        logical_item_ordinal=23, elapsed_ms=31,
    )
    assert [part.kind for part in parts] == [
        "text", "reasoning", "reasoning_summary", "reasoning_encrypted",
        "reasoning_encrypted", "text", "text",
    ]
    assert [part.text for part in parts[:-1]] == ["开场", "分析正文", "分析摘要", "", "", "[拒绝]无法提供"]
    assert [part.source.content_block_index for part in parts] == [0, 1, 1, 1, 1, 2, 3]
    assert [part.source.item_index for part in parts] == [None, 0, 0, 0, 1, None, None]
    assert [part.source.part_ordinal for part in parts] == list(range(7))
    assert len({part.part_id for part in parts}) == 7
    assert all(part.source.item_id == internal_structured_content.item_id for part in parts)
    assert all(part.source.logical_item_ordinal == 23 and part.source.elapsed_ms == 31 for part in parts)
    assert all(part.partial and part.status == "cancelled" and part.completion_reason == "length" for part in parts)
    assert len(parts[-1].text) == 65536 and parts[-1].truncated
    assert not any("密文" in part.model_dump_json() for part in parts)


@pytest.mark.parametrize("include,ordinals", [
    ("final_response", [0, 5, 6]),
    ("reasoning_detail", [1, 2]),
    ("reasoning_summary", [2]),
    ("encrypted_reasoning_meta", [3, 4]),
    ("internal", []),
])
def test_internal_structured_content_include_keeps_stable_part_ids(
    internal_structured_content: CanonicalItemRecord, include: str, ordinals: list[int],
) -> None:
    parts = response_parts_from_internal_item(
        internal_structured_content, include=frozenset({include}),
        logical_item_ordinal=23, elapsed_ms=31,
    )
    assert [part.source.part_ordinal for part in parts] == ordinals
    assert [part.part_id for part in parts] == [f"{internal_structured_content.item_id}:part:{ordinal}" for ordinal in ordinals]


def test_tool_result_truncation_does_not_join_full_input() -> None:
    content_chunks = ["x" * (1024 * 1024) for _ in range(16)]
    records = [
        _record(
            1,
            {"type": "tool", "data": {"tool_call_id": "call-large", "content": content_chunks}},
        )
    ]
    projection = _projection(
        _activity(
            "large-result",
            1,
            "tool_result",
            tool_call_id="call-large",
            message_sequence=1,
            result_message_sequence=1,
        )
    )

    tracemalloc.start()
    try:
        parts = response_parts_from_records(
            records,
            projection=projection,
            mode="detail",
            include=frozenset({"tool_result"}),
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(parts) == 1
    assert parts[0].result == "x" * 65536
    assert parts[0].truncated
    assert peak_bytes < 2 * 1024 * 1024


@pytest.mark.parametrize(("first", "second"), [
    ("分析 A", "分析 B"),
    ("相同分析", "相同分析"),
    ("分析", "分析后继"),
    (" 前文 \n", "\t 后文 "),
])
def test_internal_anonymous_thinking_keeps_each_canonical_block(
    first: str, second: str,
) -> None:
    content = [
        {"type": "thinking", "thinking": first, "signature": "signature-a"},
        {"type": "text", "text": "中间回答"},
        {"type": "thinking", "thinking": second, "signature": "signature-b"},
    ]
    canonical = canonicalize_ai_message(AIMessage(content=content))
    assert canonical.content == content
    item = _canonical_item(
        item_id="separate-thinking", item_sequence=20,
        semantic_kind="assistant_output", payload_kind="structured_content",
        payload=canonical.content,
    )
    parts = response_parts_from_internal_item(
        item, include=frozenset({"text", "reasoning_detail"}),
        logical_item_ordinal=24, elapsed_ms=32,
    )
    assert [(part.kind, part.text) for part in parts] == [
        ("reasoning", first), ("text", "中间回答"), ("reasoning", second),
    ]
    assert [(part.source.content_block_index, part.source.item_index) for part in parts] == [(0, 0), (1, None), (2, 0)]
    assert [part.part_id for part in parts] == [f"separate-thinking:part:{index}" for index in range(3)]
    assert item.payload == content


@pytest.fixture
def internal_mixed_reasoning_content() -> CanonicalItemRecord:
    content = validate_content_blocks([
        {"type": "reasoning_content", "reasoning_content": " 第一段 \n"},
        {"type": "text", "text": "正文"},
        {"type": "thinking", "thinking": "第二段", "signature": "sig-mixed"},
        {"type": "redacted_thinking", "data": "密文-redacted"},
        {"type": "reasoning_items", "reasoning_items": [
            {"id": "shared-reasoning", "type": "reasoning",
             "content": [{"type": "reasoning_text", "text": " 逐字正文 \n"}, {"type": "text", "text": "\t续段 "}],
             "summary": [{"type": "summary_text", "text": " 摘要 "}], "encrypted_content": "密文-inner"},
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "匿名同文"}]},
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "匿名同文"}]},
        ]},
        {"type": "output_text", "text": "间隔正文"},
        {"type": "reasoning_items", "reasoning_items": [
            {"id": "shared-reasoning", "type": "reasoning", "reasoning": " 重复 ID 另一块 ",
             "summary": " 同一 ID 摘要 ", "encrypted_content": "密文-later"},
        ]},
        {"type": "reasoning", "id": "direct-reasoning",
         "content": [{"type": "reasoning_text", "text": " 直接 reasoning "}],
         "summary": [{"type": "summary_text", "text": " 直接摘要 "}], "encrypted_content": "密文-direct"},
        {"type": "text", "text": "末尾正文"},
    ])
    return _canonical_item(
        item_id="mixed-reasoning", item_sequence=21,
        semantic_kind="assistant_output", payload_kind="structured_content", payload=content,
    )


def test_internal_mixed_reasoning_preserves_all_source_coordinates(
    internal_mixed_reasoning_content: CanonicalItemRecord,
) -> None:
    parts = response_parts_from_internal_item(
        internal_mixed_reasoning_content,
        include=frozenset({"text", "reasoning_detail", "encrypted_reasoning_meta"}),
        logical_item_ordinal=25, elapsed_ms=33,
    )
    assert [(part.kind, part.text, part.carrier_type, part.source.content_block_index, part.source.item_index) for part in parts] == [
        ("reasoning", " 第一段 \n", "reasoning_content", 0, 0),
        ("text", "正文", None, 1, None),
        ("reasoning", "第二段", "thinking", 2, 0),
        ("reasoning_encrypted", "", "redacted_thinking", 3, 0),
        ("reasoning", " 逐字正文 \n\t续段 ", "reasoning_items", 4, 0),
        ("reasoning_summary", " 摘要 ", "reasoning_items", 4, 0),
        ("reasoning_encrypted", "", "reasoning_items", 4, 0),
        ("reasoning", "匿名同文", "reasoning_items", 4, 1),
        ("reasoning", "匿名同文", "reasoning_items", 4, 2),
        ("text", "间隔正文", None, 5, None),
        ("reasoning", " 重复 ID 另一块 ", "reasoning_items", 6, 0),
        ("reasoning_summary", " 同一 ID 摘要 ", "reasoning_items", 6, 0),
        ("reasoning_encrypted", "", "reasoning_items", 6, 0),
        ("reasoning", " 直接 reasoning ", "reasoning", 7, 0),
        ("reasoning_summary", " 直接摘要 ", "reasoning", 7, 0),
        ("reasoning_encrypted", "", "reasoning", 7, 0),
        ("text", "末尾正文", None, 8, None),
    ]
    assert [part.source.part_ordinal for part in parts] == list(range(17))
    assert len({part.part_id for part in parts}) == 17
    assert all(part.source.item_id == internal_mixed_reasoning_content.item_id for part in parts)
    assert all(part.source.logical_item_ordinal == 25 and part.source.elapsed_ms == 33 for part in parts)
    assert not any("密文" in part.model_dump_json() for part in parts)


@pytest.mark.parametrize(("include", "ordinals"), [
    ("final_response", [1, 9, 16]),
    ("reasoning_detail", [0, 2, 4, 5, 7, 8, 10, 11, 13, 14]),
    ("reasoning_summary", [5, 11, 14]),
    ("encrypted_reasoning_meta", [3, 6, 12, 15]),
    ("internal", []),
])
def test_internal_mixed_reasoning_include_keeps_part_identity(
    internal_mixed_reasoning_content: CanonicalItemRecord, include: str, ordinals: list[int],
) -> None:
    parts = response_parts_from_internal_item(
        internal_mixed_reasoning_content, include=frozenset({include}),
        logical_item_ordinal=25, elapsed_ms=33,
    )
    assert [part.source.part_ordinal for part in parts] == ordinals
    assert [part.part_id for part in parts] == [f"mixed-reasoning:part:{ordinal}" for ordinal in ordinals]


def test_provider_reasoning_projection_keeps_merged_cleaned_metadata() -> None:
    content = [
        {"type": "reasoning_content", "reasoning_content": " 先 \n"},
        {"type": "text", "text": "正文"},
        {"type": "thinking", "thinking": "先扩展", "signature": "signature"},
        {"type": "reasoning_items", "reasoning_items": [
            {"type": "reasoning", "id": "provider-item", "reasoning": " 先扩展 ",
             "summary": [{"type": "summary_text", "text": " 摘要 "}], "encrypted_content": "sealed"},
        ]},
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "reasoning", "id": "provider-item",
         "content": [{"type": "reasoning_text", "text": " 先扩展后继 "}]},
    ]
    assert reasoning_projection_rows(content) == [{
        "kind": "reasoning", "text": "先扩展后继", "reasoning_text": "先扩展后继",
        "summary_text": "摘要", "content_block_index": 0, "item_index": 0,
        "carrier_type": "reasoning_content+thinking+reasoning_items+redacted_thinking",
        "signature_present": True, "item_id": "provider-item",
        "encrypted_length": len("sealed") + len("opaque"),
        "encrypted_hash": hashlib.sha256(b"sealed").hexdigest(),
    }]


def test_response_coordinates_follow_view_membership_instead_of_physical_sequence() -> None:
    projection = _projection(
        _activity("physical-first", 2, "reasoning", logical_item_ordinal=30, text="后展示"),
        _activity("physical-later", 7, "reasoning", logical_item_ordinal=4, text="先展示"),
    )
    parts = response_parts_from_records(
        [], projection=projection, mode="summary", include=frozenset({"thinking", "final_response"})
    )
    assert [part.source.item_sequence for part in parts] == [7, 8, 2]
    assert [part.source.logical_item_ordinal for part in parts] == [4, 20, 30]
    assert [part.source.elapsed_ms for part in parts] == [10, 0, 10]


@pytest.mark.parametrize("ordinal", [None, -1, True, "4"])
def test_activity_cannot_invent_missing_or_invalid_membership_ordinal(ordinal: object) -> None:
    with pytest.raises(TypeError, match="canonical identity/order/time"):
        response_parts_from_records(
            [], projection=_projection(_activity("no-membership", 19, "reasoning", logical_item_ordinal=ordinal)),
            mode="summary", include=frozenset({"thinking"}),
        )


def test_internal_compaction_keeps_its_kind_and_canonical_time() -> None:
    item = _canonical_item(
        item_id="internal-compaction", item_sequence=41,
        semantic_kind="compaction_summary", payload_kind="summary",
        payload={"summary": "压缩结果", "summary_id": "summary-1", "view_revision": "view-1"},
    )
    parts = response_parts_from_internal_item(
        item, include=frozenset({"thinking"}), logical_item_ordinal=6, elapsed_ms=4321,
    )
    assert len(parts) == 1
    assert parts[0].kind == "compaction_summary"
    assert parts[0].carrier_type == "compaction_summary"
    assert parts[0].text == "压缩结果"
    assert parts[0].source.item_sequence == 41
    assert parts[0].source.logical_item_ordinal == 6
    assert parts[0].source.elapsed_ms == 4321


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
                    "response_metadata": {"model_call_id": "model-call"},
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
                model_call_id="model-call",
            )
        ),
        mode="detail",
        include=frozenset({"tool_call"}),
    )

    assert parts[0].arguments == (
        '{"cmd": "pwd", "login": true, "yield_time_ms": 10000}'
    )


def test_detail_matches_scoped_tool_call_to_raw_result_id() -> None:
    records = [
        _record(
            10,
            {
                "type": "ai",
                "data": {
                    "content": [],
                    "response_metadata": {"model_call_id": "model-call"},
                    "tool_calls": [
                        {
                            "id": "call-pwd",
                            "name": "exec_command",
                            "args": {"cmd": "pwd"},
                        }
                    ],
                },
            },
        ),
        _record(
            11,
            {
                "type": "tool",
                "data": {
                    "tool_call_id": "call-pwd",
                    "response_metadata": {"model_call_id": "model-call"},
                    "content": '{"output":"/workspace","exit_code":0}',
                },
            },
        ),
    ]
    parts = response_parts_from_records(
        records,
        projection=_projection(
            _activity(
                "scoped-tool-call",
                21,
                "tool_call",
                tool_call_id="model-call:tool-call:call-pwd",
                tool_name="exec_command",
                model_call_id="model-call",
                assistant_message_sequence=10,
                call_index=0,
            ),
            _activity(
                "raw-tool-result",
                22,
                "tool_result",
                tool_call_id="call-pwd",
                tool_name="exec_command",
                model_call_id="model-call",
                assistant_message_sequence=10,
                call_index=0,
                result_message_sequence=11,
            ),
        ),
        mode="detail",
        include=frozenset({"tool_call", "tool_result"}),
    )

    assert parts[0].status == "completed"
    assert parts[0].outcome_unknown is False
    assert parts[1].result == '{"output":"/workspace","exit_code":0}'


def test_detail_does_not_guess_arguments_when_raw_tool_call_id_is_ambiguous() -> None:
    records = [
        _record(
            10,
            {
                "type": "ai",
                "data": {
                    "content": [],
                    "response_metadata": {"model_call_id": "model-call"},
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
                    "response_metadata": {"model_call_id": "model-call"},
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
                model_call_id="model-call",
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
