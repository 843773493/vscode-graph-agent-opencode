from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage

from app.services.mapping.itemized.provider_history import public_message_record
from app.services.mapping.message_content import MessageContentProjectionMixin


@pytest.fixture
def native_message() -> AIMessage:
    return AIMessage(
        id="resp-original",
        content=[
            {"type": "reasoning_items", "reasoning_items": [
                {"type": "reasoning", "id": "rs-original", "content": [
                    {"type": "reasoning_text", "text": " 第一段\n"},
                    {"type": "reasoning_text", "text": "第二段 "},
                ], "summary": [{"type": "summary_text", "text": "独立摘要"}],
                 "encrypted_content": "opaque-fixture", "extension": {"vendor": "test"}},
            ]},
            {"type": "text", "text": "回答"},
        ],
        response_metadata={"message_id": "resp-original", "content_part_refs": [
            {"id": "part-reasoning", "index": 0, "type": "reasoning_items"},
            {"id": "part-answer", "index": 1, "type": "text"},
        ]},
    )


def test_agent_record_keeps_native_layout_and_independent_content(native_message):
    original = deepcopy(native_message.content)
    record = MessageContentProjectionMixin._message_to_agent_state_record(native_message)
    assert record["content"] == original
    assert record["response_metadata"] == {**native_message.response_metadata, "phase": "final_answer"}
    native_message.content.clear()
    assert record["content"] == original


def test_lightweight_display_reads_nested_reasoning_without_protected_body(native_message):
    extracted = MessageContentProjectionMixin._extract_content(native_message)
    assert extracted == {
        "content": "回答", "reasoning_id": None,
        "content_blocks": [
            {"type": "reasoning", "reasoning": " 第一段\n第二段 ", "id": "part-reasoning", "index": 0},
            {"type": "text", "text": "回答", "id": "part-answer", "index": 1},
        ],
    }


def test_public_record_hides_only_recovery_payload_and_does_not_mutate_source(native_message):
    record = MessageContentProjectionMixin._message_to_agent_state_record(native_message)
    original = deepcopy(record)
    public = public_message_record(record)
    expected = deepcopy(original)
    content = expected["content"]
    assert isinstance(content, list)
    inner = content[0]["reasoning_items"][0]
    encrypted = inner.pop("encrypted_content")
    inner.update(encrypted_length=len(encrypted), encrypted_hash=sha256(encrypted.encode()).hexdigest())
    assert public == expected
    assert record == original
    assert public_message_record(public) == public


@pytest.mark.parametrize("block,expected", [
    ({"type": "thinking", "thinking": " 思考 ", "signature": "signature-fixture"},
     {"type": "thinking", "thinking": " 思考 ", "signature_present": True}),
    ({"type": "redacted_thinking", "data": "sealed-fixture"},
     {"type": "redacted_thinking", "encrypted_length": 14,
      "encrypted_hash": sha256(b"sealed-fixture").hexdigest()}),
    ({"type": "text", "text": "回答", "encrypted_content": "用户自有字段"},
     {"type": "text", "text": "回答", "encrypted_content": "用户自有字段"}),
])
def test_public_record_preserves_native_block_identity(block, expected):
    record = {"content": [{"id": "part-original", "index": 0, **block}]}
    assert public_message_record(record) == {
        "content": [{"id": "part-original", "index": 0, **expected}],
    }
