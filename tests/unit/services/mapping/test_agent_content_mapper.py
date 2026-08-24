from __future__ import annotations

import pytest

from app.services.mapping.agent_content_mapper import (
    extract_agent_stream_content_parts,
    extract_reasoning_summary,
    split_agent_content,
)


def test_split_agent_content_separates_standard_blocks():
    reasoning, text = split_agent_content(
        [
            {"type": "reasoning", "reasoning": "先分析"},
            {"type": "text", "text": "最终回答"},
        ]
    )

    assert reasoning == "先分析"
    assert text == "最终回答"


def test_split_agent_content_reads_direct_reasoning_and_thinking_blocks():
    reasoning, text = split_agent_content(
        [
            {
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "先分析"}],
            },
            {"type": "thinking", "thinking": "再确认"},
            {"type": "text", "text": "最终回答"},
        ]
    )

    assert reasoning == "先分析再确认"
    assert text == "最终回答"


def test_split_agent_content_treats_string_as_visible_text():
    reasoning, text = split_agent_content("普通正文")

    assert reasoning == ""
    assert text == "普通正文"


def test_split_agent_content_converts_refusal_to_visible_text():
    reasoning, text = split_agent_content([{"type": "refusal", "refusal": "无法回答"}])

    assert reasoning == ""
    assert text == "[拒绝]无法回答"


def test_extract_reasoning_summary_supports_summary_text_entries():
    assert (
        extract_reasoning_summary(
            [
                {"type": "summary_text", "text": "片段一"},
                "片段二",
                {"type": "other", "ignored": True},
            ]
        )
        == "片段一片段二"
    )


def test_extract_agent_stream_parts_preserves_authoritative_identity():
    parts = extract_agent_stream_content_parts(
        [
            {
                "type": "reasoning",
                "reasoning": "分析",
                "id": "part_reasoning",
                "index": 0,
            },
            {
                "type": "text",
                "text": "回答",
                "id": "part_answer",
                "index": 1,
            },
        ]
    )

    assert [(part.part_id, part.index, part.kind, part.text) for part in parts] == [
        ("part_reasoning", 0, "reasoning", "分析"),
        ("part_answer", 1, "markdown", "回答"),
    ]


def test_extract_agent_stream_parts_preserves_response_item_extras():
    response_item = {
        "type": "reasoning",
        "encrypted_content": "encrypted-reasoning",
        "summary": [],
    }
    parts = extract_agent_stream_content_parts(
        [
            {
                "type": "reasoning",
                "reasoning": "",
                "id": "part_reasoning",
                "index": 0,
                "extras": {"response_item": response_item},
            }
        ]
    )

    assert parts[0].extras == {"response_item": response_item}


def test_extract_agent_stream_parts_accepts_all_canonical_reasoning_carriers():
    parts = extract_agent_stream_content_parts(
        [
            {
                "type": "reasoning_content",
                "reasoning_content": "直接推理",
                "id": "part-content",
                "index": 0,
            },
            {
                "type": "reasoning_items",
                "reasoning_items": [
                    {"type": "reasoning", "summary": [{"text": "摘要"}]}
                ],
                "id": "part-items",
                "index": 1,
            },
            {
                "type": "redacted_thinking",
                "data": "sealed",
                "id": "part-redacted",
                "index": 2,
            },
            {
                "type": "text",
                "text": "回答",
                "id": "part-text",
                "index": 3,
            },
        ]
    )

    assert [part.block_type for part in parts] == [
        "reasoning_content",
        "reasoning_items",
        "redacted_thinking",
        "text",
    ]
    assert [part.kind for part in parts] == [
        "reasoning",
        "reasoning",
        "reasoning",
        "markdown",
    ]
    assert [part.text for part in parts] == ["直接推理", "摘要", "", "回答"]


def test_extract_agent_stream_parts_rejects_text_without_part_identity():
    with pytest.raises(ValueError, match="缺少权威 part id"):
        extract_agent_stream_content_parts([{"type": "text", "text": "回答"}])

    with pytest.raises(TypeError, match="必须是带 id/index"):
        extract_agent_stream_content_parts("回答")
