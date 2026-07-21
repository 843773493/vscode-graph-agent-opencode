from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import message_chunk_to_message

from app.agents.agent_factory import build_model_from_provider
from app.agents.providers.litellm_chat import (
    BoxteamLiteLLMChatModel,
    _StreamPartState,
)
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.upstream_request_trace import (
    UpstreamRequestTraceCallback,
    begin_upstream_capture,
    end_upstream_capture,
)
from app.services.orchestration.agent_stream_helpers import is_tracked_chat_model_event


def _provider(api_mode: str) -> dict[str, str]:
    return {
        "id": "provider-test",
        "endpoint": "https://example.com/v1",
        "model": "test-model",
        "api_key": "test-key",
        "custom_llm_provider": "openai",
        "api_mode": api_mode,
    }


def test_chat_completions_mode_uses_litellm_chat_model():
    model = build_model_from_provider(_provider("chat_completions"), {})
    assert isinstance(model, BoxteamLiteLLMChatModel)


def test_chat_completions_mode_forwards_stable_prompt_cache_key():
    provider = _provider("chat_completions")
    provider["capabilities"] = ["prompt_cache_key"]
    model = build_model_from_provider(
        provider,
        {},
        prompt_cache_key="session-chat-cache",
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model._client_params["extra_body"] == {
        "prompt_cache_key": "session-chat-cache"
    }


def test_chat_completions_mode_omits_cache_key_without_capability():
    model = build_model_from_provider(
        _provider("chat_completions"),
        {},
        prompt_cache_key="session-chat-cache",
    )

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert "extra_body" not in model._client_params


def test_chat_completions_reasoning_replay_capability_configures_model():
    provider = _provider("chat_completions")
    provider["capabilities"] = ["reasoning_content_replay"]

    model = build_model_from_provider(provider, {})

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.reasoning_content_replay is True


def test_responses_mode_uses_encrypted_reasoning_and_stable_cache_key():
    model = build_model_from_provider(
        _provider("responses"),
        {},
        prompt_cache_key="session-123",
    )
    assert isinstance(model, BoxteamOpenAIResponsesModel)
    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert is_tracked_chat_model_event(type(model).__name__)
    assert model.responses_store is False
    assert model.responses_include == ["reasoning.encrypted_content"]
    assert model._client_params["prompt_cache_key"] == "session-123"


def test_responses_history_replays_encrypted_reasoning_without_server_id():
    from langchain_core.messages import AIMessage

    model = build_model_from_provider(_provider("responses"), {})
    message = AIMessage(
        content=[
            {
                "type": "reasoning",
                "reasoning": "摘要",
                "id": "part-local",
                "index": 0,
                "extras": {
                    "response_item": {
                        "type": "reasoning",
                        "id": "rs_server",
                        "status": "completed",
                        "encrypted_content": "encrypted-reasoning",
                        "summary": [],
                    }
                },
            }
        ]
    )
    payload = model._responses_payload([message], None, {})
    assert payload["input"] == [
        {
            "type": "reasoning",
            "encrypted_content": "encrypted-reasoning",
            "summary": [],
        }
    ]


def test_responses_payload_converts_image_and_replays_encrypted_reasoning():
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses"), {})
    history = [
        HumanMessage(
            content=[
                {"type": "text", "text": "描述图片"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,eA=="},
                },
            ]
        ),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "reasoning": "图片分析摘要",
                    "extras": {
                        "response_item": {
                            "type": "reasoning",
                            "encrypted_content": "encrypted-image-reasoning",
                            "summary": [],
                        }
                    },
                },
                {"type": "text", "text": "图片中有测试图案"},
            ]
        ),
    ]

    payload = model._responses_payload(history, None, {})

    assert payload["input"][0] == {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "描述图片"},
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,eA==",
            },
        ],
    }
    assert payload["input"][1] == {
        "type": "reasoning",
        "encrypted_content": "encrypted-image-reasoning",
        "summary": [],
    }
    assert payload["input"][2] == {
        "type": "message",
        "role": "assistant",
        "id": None,
        "content": [
            {
                "type": "output_text",
                "text": "图片中有测试图案",
                "annotations": [],
            }
        ],
    }


def test_responses_output_normalizes_encrypted_reasoning_into_extras():
    block = BoxteamOpenAIResponsesModel._normalize_response_block(
        {
            "type": "reasoning",
            "id": "rs_server",
            "status": "completed",
            "encrypted_content": "encrypted-reasoning",
            "summary": [{"type": "summary_text", "text": "摘要"}],
        }
    )
    assert block == {
        "type": "reasoning",
        "reasoning": "摘要",
        "id": "rs_server",
        "extras": {
            "response_item": {
                "type": "reasoning",
                "encrypted_content": "encrypted-reasoning",
                "summary": [{"type": "summary_text", "text": "摘要"}],
            }
        },
    }


def test_responses_stream_keeps_one_portable_reasoning_item() -> None:
    model = BoxteamOpenAIResponsesModel(
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
    )
    part_state = _StreamPartState()
    indexes = (-1, -1, -1)
    chunks = []
    for event_type, encrypted_content in (
        ("response.output_item.added", None),
        ("response.output_item.done", "encrypted-reasoning"),
    ):
        item = {
            "type": "reasoning",
            "id": "rs_test",
            "summary": [],
        }
        if encrypted_content is not None:
            item["encrypted_content"] = encrypted_content
        event = SimpleNamespace(type=event_type, output_index=0, item=item)
        index, output_index, sub_index, chunk = model._convert_response_event(
            event,
            current_index=indexes[0],
            current_output_index=indexes[1],
            current_sub_index=indexes[2],
            part_state=part_state,
            original_schema=None,
        )
        indexes = (index, output_index, sub_index)
        assert chunk is not None
        chunks.append(chunk.message)

    message = message_chunk_to_message(chunks[0] + chunks[1])
    assert isinstance(message.content, list)
    assert len(message.content) == 1
    reasoning = message.content[0]
    assert reasoning["type"] == "reasoning"
    assert reasoning["reasoning"] == ""
    assert reasoning["extras"]["response_item"] == {
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "encrypted-reasoning",
    }


def test_responses_upstream_trace_uses_final_payload_when_litellm_input_is_empty():
    callback = UpstreamRequestTraceCallback(
        fallback_request={
            "model": "gpt-5.6-luna",
            "input": [{"type": "message", "role": "user", "content": "hello"}],
            "include": ["reasoning.encrypted_content"],
            "store": False,
            "api_key": "secret",
        }
    )
    token = begin_upstream_capture()
    callback.log_pre_api_call(
        "gpt-5.6-luna",
        None,
        {
            "litellm_call_id": "responses-1",
            "call_type": "aresponses",
            "custom_llm_provider": "openai",
            "additional_args": {"complete_input_dict": {}},
        },
    )
    attempts = end_upstream_capture(token)

    assert attempts[0]["request"]["input"][0]["role"] == "user"
    assert attempts[0]["request"]["include"] == ["reasoning.encrypted_content"]
    assert attempts[0]["request"]["api_key"] == "[REDACTED]"
