from __future__ import annotations

import pytest

from app.testing.model_stream import (
    ModelStreamAssetError,
    ModelStreamProtocolError,
    StreamFrame,
    get_protocol_codec,
)


def test_responses_codec_preserves_event_and_json_payload_on_wire() -> None:
    codec = get_protocol_codec("openai_responses_sse")
    frame = StreamFrame(
        kind="data",
        encoding="json",
        event="response.output_text.delta",
        payload={
            "type": "response.output_text.delta",
            "delta": "文本",
            "provider_unknown": {"kept": True},
        },
    )

    wire = codec.encode(frame)

    assert b"event: response.output_text.delta\n" in wire
    assert b'"provider_unknown":{"kept":true}' in wire
    decoded = codec.decode(
        event_name="response.output_text.delta",
        data='{"type":"response.output_text.delta","delta":"文本"}',
    )
    assert decoded.kind == "data"
    assert decoded.event == "response.output_text.delta"
    assert decoded.payload == {
        "type": "response.output_text.delta",
        "delta": "文本",
    }


def test_responses_codec_recognizes_completed_terminal() -> None:
    codec = get_protocol_codec("openai_responses_sse")
    frame = codec.decode(
        event_name="response.completed",
        data='{"type":"response.completed","response":{"id":"resp-1"}}',
    )

    assert frame.kind == "done"
    assert codec.is_terminal(frame)


def test_responses_codec_rejects_mismatched_terminal_payload() -> None:
    codec = get_protocol_codec("openai_responses_sse")
    frame = StreamFrame(
        kind="done",
        encoding="json",
        event="response.completed",
        payload={"type": "response.incomplete"},
    )

    with pytest.raises(ModelStreamProtocolError, match="event 与 payload.type 不一致"):
        codec.validate_frame(frame, label="invalid-responses")


def test_anthropic_codec_is_registered_but_not_runtime_supported() -> None:
    codec = get_protocol_codec("anthropic_messages_sse")

    with pytest.raises(ModelStreamProtocolError, match="anthropic_messages_sse.*暂未实现"):
        codec.require_runtime()


def test_unknown_protocol_fails_without_chat_fallback() -> None:
    with pytest.raises(ModelStreamAssetError, match="不支持的 model stream protocol"):
        get_protocol_codec("unknown_provider_sse")
