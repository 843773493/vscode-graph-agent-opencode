from __future__ import annotations

from app.services.mapping.agent_content_mapper import (
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
