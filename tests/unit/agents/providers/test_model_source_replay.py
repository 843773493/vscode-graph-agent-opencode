"""模型来源穿过 canonical 恢复后，私有推理只回放给原模型与连接。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.providers.wire_finalize import ChatWireProfile, finalize_chat_messages
from app.domain.itemized.identity.model_source import ModelSource
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.mapping.itemized.canonical_langchain import project_canonical_items
from app.services.mapping.itemized.provider_request import project_ai_message_content


@pytest.fixture
def source() -> ModelSource:
    return ModelSource(
        gateway_id="gateway-a", workspace_id="workspace-a", provider_id="service",
        model_id="main", protocol="responses", endpoint="https://example.com/v1",
        model="upstream-model", authentication="secret:sec_00000000000000000000000000000001",
    )


@pytest.fixture
def assistant(source: ModelSource) -> AIMessage:
    return AIMessage(
        id="message-a", content=[
            {"type": "reasoning_items", "reasoning_items": [{
                "type": "reasoning", "encrypted_content": "sealed-private-data",
                "summary": [{"type": "summary_text", "text": "安全摘要"}],
            }]},
            {"type": "text", "text": "可见回答"},
        ],
        tool_calls=[{"name": "read_file", "args": {"path": "file.txt"}, "id": "call-a"}],
        response_metadata={"model_source": source.to_dict(), "model_call_id": "model-call-a"},
    )


@pytest.mark.parametrize("field", [
    "gateway_id", "workspace_id", "provider_id", "model_id", "protocol", "endpoint", "model", "authentication",
])
def test_every_source_coordinate_prevents_private_replay(
    source: ModelSource, assistant: AIMessage, field: str,
) -> None:
    original = deepcopy(assistant)
    projection = project_ai_message_content(
        assistant.content, response_metadata=assistant.response_metadata,
        target_source=replace(source, **{field: "other-source"}),
        target_capabilities={"reasoning_items", "encrypted_reasoning_replay"},
    )
    assert projection["content"] == [{"type": "text", "text": "可见回答"}]
    assert projection["reasoning_items"] == [{
        "type": "reasoning", "summary": [{"type": "summary_text", "text": "安全摘要"}],
    }]
    assert projection["diagnostics"] == [{"status": "not_sent", "detail": "different_model_source"}]
    assert assistant == original


@pytest.mark.parametrize("metadata", [{}, {"source_provider": "service"}, {"model_source": {"provider_id": "service"}}])
def test_incomplete_history_never_guesses_source(
    source: ModelSource, assistant: AIMessage, metadata: dict[str, object],
) -> None:
    projection = project_ai_message_content(
        assistant.content, response_metadata=metadata, target_source=source,
        target_capabilities={"reasoning_items", "encrypted_reasoning_replay"},
    )
    assert "sealed-private-data" not in repr(projection)
    assert "安全摘要" in repr(projection)
    assert projection["diagnostics"] == [{"status": "not_sent", "detail": "missing_model_source"}]


def test_canonical_roundtrip_preserves_source_and_tool_pairing(source: ModelSource, assistant: AIMessage) -> None:
    codec = LangChainMessageCodec()
    items = codec.items_for_message(
        assistant, item_sequence=1, message_id=assistant.id, turn_id="turn-a",
        execution_id="execution-a", timestamp="2026-11-17T00:00:00Z", model_call_id="model-call-a",
    )
    restored = codec.from_dict(codec.project_message(items))
    history, = project_canonical_items(items, preserve_order=True)
    for message in (restored, history):
        assert message.content == assistant.content
        assert message.tool_calls == assistant.tool_calls
        assert message.response_metadata["model_source"] == source.to_dict()
        model = BoxteamOpenAIResponsesModel(
            model="openai/upstream-model", api_key="fixture", model_source=source,
            reasoning_items_summary_replay=True, reasoning_items_encrypted_replay=True,
        )
        tool = ToolMessage(content="文件正文", tool_call_id="call-a")
        same = model._history_messages([message, tool])
        assert "sealed-private-data" in repr([part.content for part in same])
        assert same[-1].tool_call_id == "call-a"
        model.model_source = replace(source, model_id="fallback")
        other = model._history_messages([message, tool])
        assert "sealed-private-data" not in repr([part.content for part in other])
        assert "安全摘要" in repr([part.content for part in other])
        assert [call for part in other if isinstance(part, AIMessage) for call in part.tool_calls] == assistant.tool_calls
        assert other[-1].tool_call_id == "call-a"


@pytest.mark.parametrize("same_source", [True, False])
def test_signed_and_redacted_thinking_use_the_same_source_contract(source: ModelSource, same_source: bool) -> None:
    source = replace(source, protocol="anthropic_messages")
    message = AIMessage(content=[
        {"type": "thinking", "thinking": "可读推理", "signature": "private-signature"},
        {"type": "redacted_thinking", "data": "private-redacted-data"},
        {"type": "text", "text": "回答"},
    ], response_metadata={"model_source": source.to_dict()})
    encoded, = finalize_chat_messages([message], profile=ChatWireProfile(
        provider_id="service", custom_llm_provider="anthropic", model="upstream-model",
        thinking_blocks_replay=True, redacted_thinking_replay=True,
        model_source=source if same_source else replace(source, model_id="other"),
    ))
    assert ("private-signature" in repr(encoded)) is same_source
    assert ("private-redacted-data" in repr(encoded)) is same_source
    assert "可读推理" in repr(encoded) and "回答" in repr(encoded)
    assert encoded.get("response_metadata") is None


def test_summary_survives_projection_to_plain_text_model(source: ModelSource, assistant: AIMessage) -> None:
    encoded, = finalize_chat_messages([assistant], profile=ChatWireProfile(
        provider_id="service", custom_llm_provider="openai", model="upstream-model",
        model_source=replace(source, model_id="plain", protocol="chat_completions"),
    ))
    assert "安全摘要" in repr(encoded) and "可见回答" in repr(encoded)
    assert "sealed-private-data" not in repr(encoded)
    assert encoded["tool_calls"][0]["id"] == "call-a"
