from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)
from langchain_core.outputs import ChatGenerationChunk
from pydantic import ValidationError

from app.agents.providers.litellm_content import (
    build_ai_message_content,
    canonicalize_ai_message,
    project_ai_message_content,
    reasoning_projection_rows,
    visible_text,
)
from app.agents.providers.message_content_schema import validate_content_blocks


def _content() -> list[dict[str, object]]:
    return build_ai_message_content(
        "最终回答",
        reasoning_content="兼容推理",
        thinking_blocks=[
            {"type": "thinking", "thinking": "结构化推理"},
            {"type": "redacted_thinking", "data": "sealed"},
        ],
        reasoning_items=[
            {
                "type": "reasoning",
                "id": "rs-1",
                "status": "completed",
                "encrypted_content": "encrypted",
                "summary": [{"type": "summary_text", "text": "安全摘要"}],
            }
        ],
    )


def test_builder_writes_ordered_carrier_blocks_without_project_wrapper():
    content = build_ai_message_content(
        "回答",
        reasoning_content="先分析",
        reasoning_items=[
            {
                "type": "reasoning",
                "id": "rs-1",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": "摘要"}],
            }
        ],
    )

    assert content == [
        {
            "type": "reasoning_content",
            "reasoning_content": "先分析",
        },
        {
            "type": "reasoning_items",
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "status": "completed",
                    "summary": [{"type": "summary_text", "text": "摘要"}],
                }
            ],
        },
        {"type": "text", "text": "回答"},
    ]
    assert all(block.get("type") != "litellm_payload" for block in content)


def test_content_schema_preserves_carrier_order_and_unknown_provider_fields():
    content = [
        {"type": "text", "text": "先说明"},
        {"type": "reasoning_content", "reasoning_content": "再分析"},
        {
            "type": "reasoning_items",
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "provider_extension": {"trace_group": "group-1"},
                }
            ],
        },
    ]

    validated = validate_content_blocks(content)

    assert validated == content
    assert validated is not content
    assert validated[2]["reasoning_items"][0]["provider_extension"] is not content[2]["reasoning_items"][0]["provider_extension"]


def test_content_schema_rejects_top_level_reasoning_carrier_fields():
    with pytest.raises((TypeError, ValueError, ValidationError)):
        validate_content_blocks(
            [{"type": "text", "text": "正文", "reasoning_content": "越界"}]
        )


def test_builder_copies_litellm_reasoning_item_without_field_allowlist():
    provider_item = {
        "type": "reasoning",
        "id": "rs-raw",
        "status": "completed",
        "content": [
            {"type": "reasoning_text", "text": "先读取配置。"},
        ],
        "summary": [
            {"type": "summary_text", "text": "已确定检查顺序。"},
        ],
        "encrypted_content": "sealed",
        "provider_extension": {"trace_group": "reasoning-1"},
    }

    content = build_ai_message_content(
        "完成",
        reasoning_items=[provider_item],
    )

    assert content[0]["type"] == "reasoning_items"
    assert content[0]["reasoning_items"][0] == provider_item
    assert content[0]["reasoning_items"][0] is not provider_item
    assert (
        content[0]["reasoning_items"][0]["provider_extension"]
        is not provider_item["provider_extension"]
    )

    provider_item["provider_extension"]["trace_group"] = "changed-after-copy"
    assert (
        content[0]["reasoning_items"][0]["provider_extension"]["trace_group"]
        == "reasoning-1"
    )


def test_canonicalizer_keeps_ordered_carrier_content_before_persistence():
    message = AIMessage(
        content=[
            {
                "type": "reasoning_items",
                "reasoning_items": [
                    {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": "先分析"}],
                    }
                ],
            },
            {"type": "text", "text": "最终回答"},
        ]
    )

    canonical = canonicalize_ai_message(message, source_provider="backup_1")

    assert canonical.content == [
        {
            "type": "reasoning_items",
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": "先分析"}],
                }
            ],
        },
        {"type": "text", "text": "最终回答"},
    ]


def test_build_content_does_not_duplicate_carrier_already_present_in_content():
    content = build_ai_message_content(
        [
            {"type": "text", "text": "正文前"},
            {"type": "reasoning_content", "reasoning_content": "已在正文序列"},
            {"type": "text", "text": "正文后"},
        ],
        reasoning_content="独立字段重复值",
    )

    assert [block["type"] for block in content] == [
        "text",
        "reasoning_content",
        "text",
    ]
    assert content[1]["reasoning_content"] == "已在正文序列"


def test_projection_keeps_encrypted_reasoning_only_for_same_provider():
    content = build_ai_message_content(
        "最终回答",
        reasoning_items=[
            {
                "type": "reasoning",
                "id": "rs-1",
                "status": "completed",
                "encrypted_content": "encrypted",
                "summary": [{"type": "summary_text", "text": "安全摘要"}],
            }
        ],
    )
    original = repr(content)

    same = project_ai_message_content(
        content,
        target_provider="backup_4",
        target_capabilities={"reasoning_items", "encrypted_reasoning_replay"},
        response_metadata={"provider_id": "backup_4"},
    )
    assert same["content"] == [{"type": "text", "text": "最终回答"}]
    assert same["reasoning_items"] == [
        {
            "type": "reasoning",
            "encrypted_content": "encrypted",
            "summary": [{"type": "summary_text", "text": "安全摘要"}],
        }
    ]

    other = project_ai_message_content(
        content,
        target_provider="backup_2",
        target_capabilities={"reasoning_items", "reasoning_summary"},
        response_metadata={"provider_id": "backup_4"},
    )
    assert other["reasoning_items"] == [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "安全摘要"}],
        }
    ]
    assert repr(content) == original


def test_projection_removes_stream_runtime_fields_from_visible_blocks():
    projection = project_ai_message_content(
        [
            {
                "type": "text",
                "text": "回答",
                "id": "part_text",
                "index": 1,
                "extras": {"provider_part_id": "text-1"},
            }
        ],
        target_provider="backup_1",
        target_capabilities=set(),
    )

    assert projection["content"] == [{"type": "text", "text": "回答"}]


def test_reasoning_projection_contains_summary_and_encrypted_metadata_only():
    rows = reasoning_projection_rows(_content())
    assert [row["kind"] for row in rows] == ["reasoning"]
    assert rows[0]["content_block_index"] == 0
    assert rows[0]["carrier_type"] == (
        "reasoning_content+thinking+redacted_thinking+reasoning_items"
    )
    assert rows[0]["summary_text"] == "安全摘要"
    assert rows[0]["encrypted_length"] == len("sealed") + len("encrypted")
    assert "encrypted_content" not in rows[0]


def test_stream_runtime_fields_are_removed_and_provider_item_is_restored():
    message = AIMessage(
        content=[
            {
                "type": "reasoning",
                "reasoning": "先分析",
                "id": "part_abc",
                "index": 0,
                "extras": {
                    "provider_part_id": "rs-1",
                    "response_item": {
                        "type": "reasoning",
                        "id": "rs-1",
                        "status": "completed",
                        "encrypted_content": "encrypted",
                    },
                },
            },
            {"type": "text", "text": "完成", "id": "part_def", "index": 1},
        ],
    )

    canonical = canonicalize_ai_message(message, source_provider="backup_4")
    assert canonical.content == [
        {
            "type": "reasoning_items",
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "status": "completed",
                    "encrypted_content": "encrypted",
                }
            ],
        },
        {"type": "text", "text": "完成"},
    ]
    assert "reasoning_content" not in canonical.additional_kwargs
    assert "thinking_blocks" not in canonical.additional_kwargs


def test_direct_thinking_blocks_are_preserved_once():
    message = AIMessage(
        content=[
            {"type": "thinking", "thinking": "结构化分析"},
            {"type": "redacted_thinking", "data": "sealed"},
            {"type": "text", "text": "完成"},
        ]
    )

    canonical = canonicalize_ai_message(message, source_provider="anthropic-test")

    assert canonical.content == message.content


def test_stream_thinking_block_stays_a_direct_litellm_block():
    message = AIMessage(
        content=[
            {
                "type": "thinking",
                "thinking": "流式思考",
                "signature": "sig-stream",
            }
        ]
    )

    canonical = canonicalize_ai_message(message, source_provider="anthropic-test")

    assert canonical.content == [
        {
            "type": "thinking",
            "thinking": "流式思考",
            "signature": "sig-stream",
        }
    ]


def test_checkpoint_roundtrip_preserves_direct_content_and_tool_contract():
    assistant = AIMessage(
        content=_content(),
        tool_calls=[{"name": "inspect_fixture", "args": {"turn": 1}, "id": "call-1"}],
    )
    tool = ToolMessage(content="ok", tool_call_id="call-1")

    restored = messages_from_dict(
        [message_to_dict(assistant), message_to_dict(tool)]
    )
    assert restored[0].content == assistant.content
    assert restored[0].tool_calls == assistant.tool_calls
    assert restored[1].tool_call_id == "call-1"


def test_visible_text_ignores_reasoning_and_encrypted_content():
    assert visible_text(_content()) == "最终回答"


def test_streaming_invoke_canonicalizes_langchain_cache_merge(monkeypatch):
    from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel

    model = BoxteamLiteLLMChatModel(
        model="openai/test-model",
        api_key="test-key",
        api_base="https://example.com/v1",
        provider_id="backup_1",
        streaming=True,
    )

    def fake_stream(self, messages, stop=None, run_manager=None, **kwargs):
        del messages, stop, run_manager, kwargs
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=[
                    {
                        "type": "reasoning_content",
                        "reasoning_content": "先分析",
                    }
                ]
            )
        )
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=[{"type": "text", "text": "完成"}])
        )

    monkeypatch.setattr(BoxteamLiteLLMChatModel, "_stream", fake_stream)
    response = model.invoke("开始")

    assert response.content == [
        {"type": "reasoning_content", "reasoning_content": "先分析"},
        {"type": "text", "text": "完成"},
    ]
