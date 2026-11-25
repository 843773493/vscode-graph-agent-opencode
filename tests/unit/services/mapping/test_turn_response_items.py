from __future__ import annotations

import hashlib
import json
import tracemalloc
from dataclasses import replace
from datetime import datetime

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agents.providers.message_content_schema import validate_content_blocks
from app.agents.providers.response_normalization import canonicalize_ai_message
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.mapping.itemized.canonical_langchain import project_canonical_items
from app.services.mapping.itemized.history_turns.detail import build_detail
from app.services.mapping.itemized.history_turns.projection import (
    HistoryProjectionBudget,
    HistoryProjectionLimits,
    bounded_content,
    project_detail,
)
from app.services.mapping.itemized.provider_history import reasoning_projection_rows
from app.services.mapping.turn_response_items import (
    response_items_from_internal_item,
    response_items_from_records,
)

NOW = "2026-09-09T00:00:00+00:00"


def test_output_content_preserves_unknown_blocks_and_never_clips_media_references() -> None:
    content = {"type": "custom_card", "value": {"label": "模型提供的数据", "rows": [1, 2]}}
    message = AIMessage(id="structured-output", content=[content])
    item, = LangChainMessageCodec().items_for_message(message, item_sequence=1,
        message_id=message.id, turn_id=None, execution_id="execution_1", timestamp=NOW)
    assert item.projection_manifest()["output_block_type"] == "custom_card"
    part, = response_items_from_internal_item(item, include=frozenset({"text"}), logical_item_ordinal=3, elapsed_ms=0)
    assert part.kind == "content" and part.id == item.item_id and part.content == content
    assert part.text == ""
    assert project_canonical_items([item])[0].content == [content]
    budget = HistoryProjectionBudget(HistoryProjectionLimits(bytes=1, chars=1, item_chars=1))
    assert bounded_content(part.content, budget) == (None, True)
    assert budget.bytes_used == 0
    oversized = _canonical_item(item_id=item.item_id, item_sequence=1, semantic_kind="assistant_output",
        payload_kind="structured_content",
        payload=[{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 65536}}])
    bounded, = response_items_from_internal_item(oversized, include=frozenset({"text"}), logical_item_ordinal=3, elapsed_ms=0)
    assert bounded.id == item.item_id and bounded.carrier_type == "image_url"
    assert bounded.content is None and bounded.truncated


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
        "final_items": [_activity("item-final", 8, "text", logical_item_ordinal=20,
                                  text="完成", elapsed_ms=0, message_sequence=3)],
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

    parts = response_items_from_internal_item(
        item,
        include=frozenset({"final_response", "internal"}),
        logical_item_ordinal=7,
        elapsed_ms=12,
    )
    excluded = response_items_from_internal_item(
        item,
        include=frozenset({"internal"}),
        logical_item_ordinal=7,
        elapsed_ms=12,
    )

    assert len(parts) == 1
    assert parts[0].kind == "text"
    assert parts[0].projection == "summary"
    assert parts[0].id == "item-internal-output"
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

    summary = response_items_from_internal_item(
        item,
        include=frozenset({"tool_summary"}),
        logical_item_ordinal=7,
        elapsed_ms=21,
    )
    detail = response_items_from_internal_item(
        item,
        include=frozenset({"tool_call"}),
        logical_item_ordinal=7,
        elapsed_ms=21,
    )

    assert summary[0].projection == "summary"
    assert summary[0].arguments is None
    assert detail[0].projection == "detail"
    # 沿普通 Turn 的既有 JSON 格式；统一点读不能因独立执行的格式差异改写正文。
    assert detail[0].arguments == '{"path": "a.py"}'


def test_large_tool_arguments_keep_item_identity_and_report_truncation() -> None:
    message = AIMessage(id="large-call", content="", tool_calls=[{
        "id": "call-large", "name": "write_file", "args": {"content": "x" * 70000},
    }])
    item, = LangChainMessageCodec().items_for_message(message, item_sequence=1,
        message_id=message.id, turn_id=None, execution_id="execution_1", timestamp=NOW)
    part, = response_items_from_internal_item(item, include=frozenset({"tool_call"}),
        logical_item_ordinal=4, elapsed_ms=7)
    assert part.id == part.source.item_id == item.item_id
    assert part.tool_call_id == "call-large"
    assert part.arguments == json.dumps({"content": "x" * 70000}, ensure_ascii=False)[:65536]
    assert part.truncated and part.projection == "detail"
    assert project_canonical_items([item])[0].tool_calls == message.tool_calls


@pytest.mark.parametrize("kind", ["tool_call", "tool_result"])
def test_turn_tool_fields_share_the_remaining_page_budget(kind: str) -> None:
    message = (AIMessage(id="budget-call", content="", tool_calls=[{
        "id": "call-budget", "name": "read_file", "args": {"path": "文" * 200},
    }]) if kind == "tool_call" else ToolMessage(id="budget-result", content="文" * 200,
        tool_call_id="call-budget", name="read_file"))
    item, = LangChainMessageCodec().items_for_message(message, item_sequence=1,
        message_id=message.id, turn_id=None, execution_id="execution_1", timestamp=NOW)
    part, = response_items_from_internal_item(item, include=frozenset({kind}), logical_item_ordinal=4, elapsed_ms=7)
    detail = build_detail("session-budget", 1, [], turn_id="turn-budget",
        thread_id="thr_00000000700070008000000000000001", job_id="job-budget",
        root_logical_item_ordinal=0, source_view_id="view-budget", source_view_revision=1,
        projection={"created_at": NOW, "user_created_at": NOW, "updated_at": NOW},
        response_items=[part], include_final=False)
    budget = HistoryProjectionBudget(HistoryProjectionLimits(bytes=256, chars=80, item_chars=64))
    budget.add(byte_count=100, char_count=30)
    bounded = project_detail(detail, (kind,), budget)
    actual, = bounded.response_items
    values = [value for value in (actual.text, actual.arguments, actual.result) if value]
    assert sum(len(value) for value in values) <= 50
    assert sum(len(json.dumps(value, ensure_ascii=False).encode()) for value in values) <= 156
    assert budget.chars_used == 30 + sum(len(value) for value in values)
    assert budget.bytes_used == 100 + sum(len(json.dumps(value, ensure_ascii=False).encode()) for value in values)
    assert actual.truncated and bounded.detail_truncated
    assert actual.id == part.id and actual.source == part.source
    assert detail.response_items == [part] and not part.truncated


@pytest.fixture
def internal_structured_content() -> tuple[CanonicalItemRecord, ...]:
    content = [
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
    ]
    message = AIMessage(id="internal-final", content=content, response_metadata={
        "model_call_id": "internal-model", "phase": "final_answer", "finish_reason": "length",
    })
    items = LangChainMessageCodec().items_for_message(message, item_sequence=19,
        message_id=message.id, turn_id=None, execution_id="internal-execution", timestamp=NOW)
    assert project_canonical_items(items)[0].content == content
    return tuple(replace(item, status="partial") for item in items)


def test_internal_structured_content_preserves_semantic_items_and_bounds(
    internal_structured_content: tuple[CanonicalItemRecord, ...],
) -> None:
    parts = [part for index, item in enumerate(internal_structured_content)
        for part in response_items_from_internal_item(item,
            include=frozenset({"text", "reasoning_detail", "encrypted_reasoning_meta"}),
            logical_item_ordinal=23 + index, elapsed_ms=31)]
    assert [part.kind for part in parts] == ["text", "reasoning", "text", "text"]
    assert [part.text for part in parts[:-1]] == ["开场", "分析正文", "[拒绝]无法提供"]
    assert [part.source.item_id for part in parts] == [item.item_id for item in internal_structured_content]
    assert len({part.id for part in parts}) == 4
    assert [part.source.logical_item_ordinal for part in parts] == [23, 24, 25, 26]
    assert all(part.source.elapsed_ms == 31 and part.partial and part.status == "cancelled" for part in parts)
    assert len(parts[-1].text) == 65536 and parts[-1].truncated
    assert not any("密文" in part.model_dump_json() for part in parts)


@pytest.mark.parametrize("include,indices", [
    ("final_response", [0, 2, 3]),
    ("reasoning_detail", [1]),
    ("reasoning_summary", [1]),
    ("encrypted_reasoning_meta", []),
    ("internal", []),
])
def test_internal_structured_content_include_keeps_item_identity(
    internal_structured_content: tuple[CanonicalItemRecord, ...], include: str, indices: list[int],
) -> None:
    parts = [part for index, item in enumerate(internal_structured_content)
        for part in response_items_from_internal_item(item, include=frozenset({include}),
            logical_item_ordinal=23 + index, elapsed_ms=31)]
    assert [part.source.item_id for part in parts] == [internal_structured_content[index].item_id for index in indices]
    if include == "reasoning_summary":
        assert parts[0].text == "分析摘要" and parts[0].kind == "reasoning_summary"
    assert not any("密文" in part.model_dump_json() for part in parts)


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
        parts = response_items_from_records(
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
    items = LangChainMessageCodec().items_for_message(canonical, item_sequence=20,
        message_id="separate-thinking", turn_id=None, execution_id="internal-execution", timestamp=NOW)
    parts = [part for index, item in enumerate(items)
        for part in response_items_from_internal_item(item,
            include=frozenset({"text", "reasoning_detail"}),
            logical_item_ordinal=24 + index, elapsed_ms=32)]
    assert [(part.kind, part.text) for part in parts] == [
        ("reasoning", first), ("text", "中间回答"), ("reasoning", second),
    ]
    assert [part.source.item_id for part in parts] == [item.item_id for item in items]
    assert len({part.id for part in parts}) == 3
    assert project_canonical_items(items)[0].content == content


@pytest.fixture
def internal_mixed_reasoning_content() -> tuple[CanonicalItemRecord, ...]:
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
    message = AIMessage(id="mixed-reasoning", content=content, response_metadata={"model_call_id": "mixed-call"})
    items = LangChainMessageCodec().items_for_message(message, item_sequence=21,
        message_id=message.id, turn_id=None, execution_id="internal-execution", timestamp=NOW)
    assert project_canonical_items(items)[0].content == content
    return items


def test_internal_mixed_reasoning_preserves_carriers_and_source_items(
    internal_mixed_reasoning_content: tuple[CanonicalItemRecord, ...],
) -> None:
    parts = [part for index, item in enumerate(internal_mixed_reasoning_content)
        for part in response_items_from_internal_item(item,
            include=frozenset({"text", "reasoning_detail", "encrypted_reasoning_meta"}),
            logical_item_ordinal=25 + index, elapsed_ms=33)]
    assert [(part.kind, part.text, part.carrier_type) for part in parts] == [
        ("reasoning", " 第一段 \n", "reasoning_content"),
        ("text", "正文", None),
        ("reasoning", "第二段", "thinking"),
        ("reasoning_encrypted", "", "redacted_thinking"),
        ("reasoning", " 逐字正文 \n\t续段 匿名同文匿名同文", "reasoning_items"),
        ("text", "间隔正文", None),
        ("reasoning", " 重复 ID 另一块 ", "reasoning_items"),
        ("reasoning", " 直接 reasoning ", "reasoning"),
        ("text", "末尾正文", None),
    ]
    assert len({part.id for part in parts}) == 9
    assert [part.source.item_id for part in parts] == [item.item_id for item in internal_mixed_reasoning_content]
    assert [part.source.logical_item_ordinal for part in parts] == list(range(25, 34))
    assert not any("密文" in part.model_dump_json() for part in parts)


@pytest.mark.parametrize(("include", "indices"), [
    ("final_response", [1, 5, 8]),
    ("reasoning_detail", [0, 2, 4, 6, 7]),
    ("reasoning_summary", [4, 6, 7]),
    ("encrypted_reasoning_meta", [3]),
    ("internal", []),
])
def test_internal_mixed_reasoning_include_keeps_item_identity(
    internal_mixed_reasoning_content: tuple[CanonicalItemRecord, ...], include: str, indices: list[int],
) -> None:
    parts = [part for index, item in enumerate(internal_mixed_reasoning_content)
        for part in response_items_from_internal_item(item, include=frozenset({include}),
            logical_item_ordinal=25 + index, elapsed_ms=33)]
    assert [part.source.item_id for part in parts] == [internal_mixed_reasoning_content[index].item_id for index in indices]
    if include == "reasoning_summary":
        assert [part.text for part in parts] == [" 摘要 ", " 同一 ID 摘要 ", " 直接摘要 "]
    assert not any("密文" in part.model_dump_json() for part in parts)


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
    parts = response_items_from_records(
        [], projection=projection, mode="summary", include=frozenset({"thinking", "final_response"})
    )
    assert [part.source.item_sequence for part in parts] == [7, 8, 2]
    assert [part.source.logical_item_ordinal for part in parts] == [4, 20, 30]
    assert [part.source.elapsed_ms for part in parts] == [10, 0, 10]


@pytest.mark.parametrize("ordinal", [None, -1, True, "4"])
def test_activity_cannot_invent_missing_or_invalid_membership_ordinal(ordinal: object) -> None:
    with pytest.raises(TypeError, match="canonical identity/order/time"):
        response_items_from_records(
            [], projection=_projection(_activity("no-membership", 19, "reasoning", logical_item_ordinal=ordinal)),
            mode="summary", include=frozenset({"thinking"}),
        )


def test_internal_compaction_keeps_its_kind_and_canonical_time() -> None:
    item = _canonical_item(
        item_id="internal-compaction", item_sequence=41,
        semantic_kind="compaction_summary", payload_kind="summary",
        payload={"summary": "压缩结果", "summary_id": "summary-1", "view_revision": "view-1"},
    )
    parts = response_items_from_internal_item(
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

    parts = response_items_from_records(
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
    parts = response_items_from_records(
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
    assert parts[0].id == "summary-1"


def test_identical_reasoning_text_with_distinct_identity_is_preserved() -> None:
    parts = response_items_from_records(
        [],
        projection=_projection(
            _activity("reasoning-1", 2, "reasoning", text="相同正文"),
            _activity("reasoning-2", 7, "reasoning", text="相同正文"),
        ),
        mode="detail",
        include=frozenset({"reasoning_detail"}),
    )

    assert [part.id for part in parts] == [
        "reasoning-1",
        "reasoning-2",
    ]


def test_partial_output_is_not_promoted_to_final() -> None:
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
        projection = _projection(_activity("partial-output", 8, "text", text="完成",
            status="partial", completion_reason="user_interrupt"), status="cancelled")
        projection["final_items"] = []
        parts = response_items_from_records(
            records,
            projection=projection,
            mode=mode,
            include=frozenset({"final_response"}),
        )
        assert len(parts) == 1
        assert parts[0].kind == "text"
        assert parts[0].final is False
        assert parts[0].completion_reason == "user_interrupt"


@pytest.mark.parametrize("outcome", [None, "success", "failure", "cancelled", "unknown"])
def test_detail_without_result_body_keeps_only_explicit_tool_outcome(outcome: str | None) -> None:
    parts = response_items_from_records(
        [],
        projection=_projection(
            _activity(
                "tool-call-1",
                3,
                "tool_call",
                tool_call_id="call-unknown",
                tool_name="read_file",
                tool_outcome=outcome,
            )
        ),
        mode="detail",
        include=frozenset({"tool_call", "tool_result"}),
    )

    assert len(parts) == 1
    assert parts[0].status == "completed"  # 声明完整，不表示执行成功。
    assert parts[0].tool_outcome == outcome
    assert parts[0].outcome_unknown is (outcome == "unknown")


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
    parts = response_items_from_records(
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
    parts = response_items_from_records(
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
    parts = response_items_from_records(
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
    parts = response_items_from_records(
        [
            _record(5, {"type": "ai", "data": {
                "content": [{"type": "text", "text": "检查文件"}],
                "tool_calls": [{"id": "call-1", "name": "exec_command", "args": {"command": "pwd"}}],
            }}),
            _record(6, {"type": "tool", "data": {"tool_call_id": "call-1", "content": "工具结果"}}),
        ],
        projection=_projection(
            _activity("reasoning-1", 2, "reasoning", text="分析"),
            _activity(
                "tool-call-1",
                3,
                "tool_call",
                tool_call_id="call-1",
                tool_name="exec_command",
                assistant_message_sequence=5,
                call_index=0,
            ),
            _activity(
                "tool-result-1",
                4,
                "tool_result",
                tool_call_id="call-1",
                tool_name="exec_command",
                result_message_sequence=6,
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
    assert all(part.result is None and part.text == "" for part in tool_parts)


def test_tool_parts_are_detail_when_tool_payload_is_included() -> None:
    """显式请求 tool_call/tool_result 时，工具部件才标记为 detail。"""
    parts = response_items_from_records(
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
