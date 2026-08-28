from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    message_chunk_to_message,
)

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
    record_upstream_response,
)
from app.services.orchestration.agent_stream_helpers import (
    is_tracked_chat_model_event,
)


def _provider(api_mode: str) -> dict[str, object]:
    if api_mode == "responses":
        structured_api_mode: dict[str, object] = {
            "protocol": "responses",
            "model_info": {
                "supports_function_calling": True,
                "supports_reasoning": True,
            },
            "supports_reasoning": {
                "reasoning_items": {"summary": True, "encrypted_content": True}
            },
            "request_features": {"prompt_cache_key": True},
            "replay_policy": {"encrypted_content": "same_source"},
        }
    else:
        structured_api_mode = {
            "protocol": "chat_completions",
            "model_info": {
                "supports_function_calling": True,
                "supports_reasoning": True,
            },
            "supports_reasoning": {"reasoning_content": True},
        }
    return {
        "id": "provider-test",
        "endpoint": "https://example.com/v1",
        "model": "test-model",
        "api_key": "test-key",
        "custom_llm_provider": "openai",
        "api_mode": structured_api_mode,
    }


def test_chat_completions_mode_uses_litellm_chat_model():
    model = build_model_from_provider(_provider("chat_completions"), {})
    assert isinstance(model, BoxteamLiteLLMChatModel)


def test_litellm_output_projection_preserves_reasoning_and_provider_summary():
    normalized = BoxteamLiteLLMChatModel.normalize_output_content(
        [
            {"type": "reasoning", "reasoning": "模型推理"},
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "安全摘要"}],
            },
        ]
    )

    assert normalized == [
        {"type": "reasoning", "reasoning": "模型推理"},
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "安全摘要"}],
        },
    ]


def test_litellm_output_projection_preserves_redacted_reasoning_payload_marker():
    normalized = BoxteamLiteLLMChatModel.normalize_output_content(
        [
            {
                "type": "redacted_thinking",
                "encrypted_content": "provider-secret",
            }
        ]
    )

    assert normalized == [
        {
            "type": "redacted_thinking",
            "encrypted_content": "provider-secret",
        }
    ]


def test_litellm_unified_reasoning_fields_use_ordered_carrier_blocks():
    model = BoxteamLiteLLMChatModel(
        model="openai/test-model",
        api_key="test-key",
        api_base="https://example.com/v1",
    )
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "最终回答",
                    "reasoning_content": "兼容推理",
                    "thinking_blocks": [
                        {
                            "type": "thinking",
                            "thinking": "结构化推理",
                            "signature": "sig-1",
                        },
                        {"type": "redacted_thinking", "data": "sealed-1"},
                    ],
                    "reasoning_items": [
                        {
                            "type": "reasoning",
                            "id": "rs-1",
                            "encrypted_content": "encrypted-1",
                            "summary": [
                                {"type": "summary_text", "text": "安全摘要"}
                            ],
                        }
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }

    message = model._create_chat_result(response).generations[0].message

    assert isinstance(message.content, list)
    assert message.content == [
        {"type": "reasoning_content", "reasoning_content": "兼容推理"},
        {
            "type": "thinking",
            "thinking": "结构化推理",
            "signature": "sig-1",
        },
        {"type": "redacted_thinking", "data": "sealed-1"},
        {
            "type": "reasoning_items",
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "encrypted_content": "encrypted-1",
                    "summary": [{"type": "summary_text", "text": "安全摘要"}],
                }
            ],
        },
        {"type": "text", "text": "最终回答"},
    ]
    assert all(
        block.get("type") != "litellm_payload"
        for block in message.content
        if isinstance(block, dict)
    )
    assert "reasoning_content" not in message.additional_kwargs
    assert "thinking_blocks" not in message.additional_kwargs
    assert "reasoning_items" not in message.additional_kwargs


def test_chat_completions_mode_forwards_stable_prompt_cache_key():
    provider = _provider("chat_completions")
    api_mode = provider["api_mode"]
    assert isinstance(api_mode, dict)
    api_mode["request_features"] = {"prompt_cache_key": True}
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

    model = build_model_from_provider(provider, {})

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.reasoning_content_replay is True


def test_chat_completions_thinking_blocks_configures_model_projection():
    provider = _provider("chat_completions")
    provider["api_mode"] = {
        "protocol": "chat_completions",
        "model_info": {"supports_function_calling": True, "supports_reasoning": True},
        "supports_reasoning": {"thinking_blocks": True},
    }

    model = build_model_from_provider(provider, {})

    assert isinstance(model, BoxteamLiteLLMChatModel)
    assert model.reasoning_content_replay is False
    assert model.thinking_blocks_replay is True


def test_anthropic_messages_mode_uses_anthropic_adapter():
    from app.agents.providers.anthropic_messages import (
        BoxteamAnthropicMessagesModel,
    )

    provider = {
        "id": "anthropic-test",
        "endpoint": "https://example.com/anthropic",
        "model": "claude-test",
        "api_key": "test-key",
        "custom_llm_provider": "anthropic",
        "api_mode": {
            "protocol": "anthropic_messages",
            "model_info": {"supports_reasoning": True},
            "supports_reasoning": {
                "thinking_blocks": {
                    "thinking": True,
                    "redacted_thinking": True,
                }
            },
        },
    }

    model = build_model_from_provider(provider, {})

    assert isinstance(model, BoxteamAnthropicMessagesModel)
    assert model.thinking_blocks_replay is True
    assert model.redacted_thinking_replay is True


def test_anthropic_history_projects_direct_content_to_thinking_blocks():
    from langchain_core.messages import AIMessage

    from app.agents.providers.anthropic_messages import (
        BoxteamAnthropicMessagesModel,
    )
    from app.agents.providers.litellm_content import build_ai_message_content

    model = BoxteamAnthropicMessagesModel(
        model_name="claude-test",
        api_key="test-key",
        base_url="https://example.com/anthropic",
        provider_id="anthropic-test",
        thinking_blocks_replay=True,
        redacted_thinking_replay=True,
    )
    message = AIMessage(
        content=build_ai_message_content(
            "最终回答",
            source_provider="anthropic-test",
            thinking_blocks=[
                {"type": "thinking", "thinking": "分析"},
                {"type": "redacted_thinking", "data": "sealed"},
            ],
        ),
        response_metadata={"provider_id": "anthropic-test"},
    )

    projected = model._project_ai_message(message)

    assert projected.content == [
        {"type": "thinking", "thinking": "分析"},
        {"type": "redacted_thinking", "data": "sealed"},
        {"type": "text", "text": "最终回答"},
    ]


def test_anthropic_history_drops_reasoning_for_unsupported_target():
    from langchain_core.messages import AIMessage

    from app.agents.providers.anthropic_messages import (
        BoxteamAnthropicMessagesModel,
    )

    model = BoxteamAnthropicMessagesModel(
        model_name="claude-test",
        api_key="test-key",
        base_url="https://example.com/anthropic",
        provider_id="target-provider",
        thinking_blocks_replay=False,
        redacted_thinking_replay=False,
    )
    message = AIMessage(
        content=[
            {"type": "reasoning_content", "reasoning_content": "私有推理"},
            {
                "type": "reasoning_items",
                "reasoning_items": [
                    {"type": "reasoning", "content": [{"type": "text", "text": "内部项"}]},
                ],
            },
            {"type": "thinking", "thinking": "思考"},
            {"type": "redacted_thinking", "data": "sealed"},
            {"type": "text", "text": "可见回答"},
        ],
        tool_calls=[
            {"id": "call-1", "name": "read_file", "args": {"path": "a.txt"}},
        ],
        response_metadata={"provider_id": "source-provider"},
    )

    projected = model._project_ai_message(message)

    assert projected.content == [{"type": "text", "text": "可见回答"}]
    assert projected.tool_calls == message.tool_calls


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


def test_chatgpt_oauth_responses_uses_stable_litellm_session_id(monkeypatch):
    monkeypatch.setattr(
        "app.runtime.chatgpt_auth.configure_litellm_chatgpt_auth_directory",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.runtime.chatgpt_auth.ensure_chatgpt_oauth_ready",
        lambda _token_dir: None,
    )
    monkeypatch.setattr(
        "app.runtime.chatgpt_auth.ensure_litellm_chatgpt_model_capabilities",
        lambda _: False,
    )
    provider = {
        "id": "backup_4",
        "endpoint": "https://chatgpt.com/backend-api/codex",
        "model": "gpt-5.6-luna",
        "custom_llm_provider": "chatgpt",
        "api_mode": _provider("responses")["api_mode"],
        "auth": {"type": "oauth", "method": "chatgpt"},
    }

    model = build_model_from_provider(
        provider,
        {},
        prompt_cache_key="ses_chatgpt_cache_affinity",
    )

    assert isinstance(model, BoxteamOpenAIResponsesModel)
    assert model._client_params["custom_llm_provider"] == "chatgpt"
    assert model._client_params["litellm_session_id"] == "ses_chatgpt_cache_affinity"
    assert "prompt_cache_key" not in model._client_params

    payload = model._responses_payload(
        [SystemMessage(content="项目系统指令"), HumanMessage(content="用户问题")],
        None,
        {},
    )
    assert payload["instructions"] == "项目系统指令"
    assert [item.get("role") for item in payload["input"]] == ["user"]
    assert payload["litellm_session_id"] == "ses_chatgpt_cache_affinity"


def test_chatgpt_provider_rejects_missing_oauth_config():
    provider = _provider("responses")
    provider["custom_llm_provider"] = "chatgpt"

    with pytest.raises(ValueError, match="auth.type='oauth'"):
        build_model_from_provider(provider, {})


def test_responses_history_replays_encrypted_reasoning_without_server_id():
    from langchain_core.messages import AIMessage

    model = build_model_from_provider(_provider("responses"), {})
    message = AIMessage(
        content=[
            {
                "type": "reasoning",
                "id": "rs_server",
                "status": "completed",
                "encrypted_content": "encrypted-reasoning",
                "summary": [],
            }
        ],
        response_metadata={"provider_id": "provider-test"},
    )
    payload = model._responses_payload([message], None, {})
    assert payload["input"] == [
        {
            "type": "reasoning",
            "encrypted_content": "encrypted-reasoning",
            "summary": [],
        }
    ]


def test_responses_history_drops_unportable_provider_reasoning_id():
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses"), {})
    history = [
        HumanMessage(content="执行任务"),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "id": "part-local",
                    "status": "completed",
                    "summary": [],
                }
            ],
            response_metadata={"provider_id": "provider-test"},
        ),
        HumanMessage(content="空响应恢复，请继续处理"),
    ]

    payload = model._responses_payload(history, None, {})

    assert payload["input"] == [
        {"type": "message", "role": "user", "content": "执行任务"},
        {
            "type": "message",
            "role": "user",
            "content": "空响应恢复，请继续处理",
        },
    ]
    assert "provider_part_id" not in str(payload)


def test_responses_history_drops_encrypted_reasoning_from_another_provider():
    from langchain_core.messages import AIMessage, HumanMessage

    model = build_model_from_provider(_provider("responses"), {})
    history = [
        HumanMessage(content="跨模型继续"),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "encrypted_content": "foreign-provider-payload",
                    "summary": [],
                },
                {"type": "text", "text": "保留可见正文"},
            ],
            response_metadata={"provider_id": "primary"},
        ),
    ]

    payload = model._responses_payload(history, None, {})

    assert "foreign-provider-payload" not in str(payload)
    assert any(item.get("type") == "message" for item in payload["input"])


def test_responses_history_keeps_tool_call_when_dropping_unportable_reasoning():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    model = build_model_from_provider(_provider("responses"), {})
    history = [
        HumanMessage(content="读取 README"),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "id": "rs_server",
                    "status": "completed",
                    "summary": [],
                }
            ],
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"path": "README.md"},
                    "id": "call_readme",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="README 内容", tool_call_id="call_readme"),
    ]

    payload = model._responses_payload(history, None, {})

    assert "provider_part_id" not in str(payload)
    assert any(
        item.get("type") == "function_call" and item.get("call_id") == "call_readme"
        for item in payload["input"]
    )
    assert any(
        item.get("type") == "function_call_output"
        and item.get("call_id") == "call_readme"
        for item in payload["input"]
    )


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
                    "encrypted_content": "encrypted-image-reasoning",
                    "summary": [],
                },
                {"type": "text", "text": "图片中有测试图案"},
            ],
            response_metadata={"provider_id": "provider-test"},
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


def test_responses_output_keeps_direct_reasoning_item():
    block = BoxteamOpenAIResponsesModel._normalize_response_block(
        {
            "type": "reasoning",
            "id": "rs_server",
            "status": "completed",
            "content": [
                {"type": "reasoning_text", "text": "先确认范围"},
            ],
            "encrypted_content": "encrypted-reasoning",
            "summary": [{"type": "summary_text", "text": "摘要"}],
            "provider_extension": {"trace_group": "reasoning-1"},
        }
    )
    assert block == {
        "type": "reasoning",
        "id": "rs_server",
        "status": "completed",
        "content": [
            {"type": "reasoning_text", "text": "先确认范围"},
        ],
        "encrypted_content": "encrypted-reasoning",
        "summary": [{"type": "summary_text", "text": "摘要"}],
        "provider_extension": {"trace_group": "reasoning-1"},
    }


def test_responses_output_text_only_normalizes_type_without_dropping_fields():
    block = BoxteamOpenAIResponsesModel._normalize_response_block(
        {
            "type": "output_text",
            "text": "完成",
            "annotations": [{"type": "url_citation", "url": "https://example.com"}],
            "phase": "final_answer",
            "provider_extension": {"trace_group": "text-1"},
        }
    )

    assert block == {
        "type": "text",
        "text": "完成",
        "annotations": [{"type": "url_citation", "url": "https://example.com"}],
        "phase": "final_answer",
        "provider_extension": {"trace_group": "text-1"},
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
        if event_type == "response.output_item.added":
            assert chunk is None
        else:
            assert chunk is not None
            chunks.append(chunk.message)

    message = message_chunk_to_message(chunks[0])
    from app.agents.providers.litellm_content import canonicalize_ai_message

    message = canonicalize_ai_message(message, source_provider="openai")
    assert isinstance(message.content, list)
    assert len(message.content) == 1
    assert message.content[0] == {
        "type": "reasoning_items",
        "reasoning_items": [
            {
                "type": "reasoning",
                "id": "rs_test",
                "summary": [],
                "encrypted_content": "encrypted-reasoning",
            }
        ],
    }


def test_responses_summary_delta_emits_reasoning_text_block() -> None:
    model = BoxteamOpenAIResponsesModel(
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
    )
    part_state = _StreamPartState()
    event = SimpleNamespace(
        type="response.reasoning_summary_text.delta",
        item_id="rs_summary",
        delta="先读取 README",
    )

    _index, _output_index, _sub_index, chunk = model._convert_response_event(
        event,
        current_index=-1,
        current_output_index=-1,
        current_sub_index=-1,
        part_state=part_state,
        original_schema=None,
    )

    assert chunk is not None
    block = chunk.message.content[0]
    assert block["type"] == "reasoning"
    assert block["content"] == [
        {
            "type": "reasoning_text",
            "text": "先读取 README",
        }
    ]
    assert isinstance(block["id"], str)
    assert block["index"] == 0
    assert block["extras"] == {"provider_part_id": "rs_summary"}


def test_responses_empty_added_summary_does_not_emit_empty_reasoning_block() -> None:
    model = BoxteamOpenAIResponsesModel(
        model="gpt-test",
        api_key="test-key",
        custom_llm_provider="openai",
    )
    event = SimpleNamespace(
        type="response.output_item.added",
        output_index=0,
        item={
            "type": "reasoning",
            "id": "rs_empty",
            "summary": [{"type": "summary_text", "text": ""}],
        },
    )

    _index, _output_index, _sub_index, chunk = model._convert_response_event(
        event,
        current_index=-1,
        current_output_index=-1,
        current_sub_index=-1,
        part_state=_StreamPartState(),
        original_schema=None,
    )

    assert chunk is None


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


def test_responses_stream_terminal_event_records_complete_upstream_response():
    callback = UpstreamRequestTraceCallback()
    token = begin_upstream_capture()
    callback.log_pre_api_call(
        "gpt-5.6-luna",
        None,
        {
            "litellm_call_id": "responses-stream-1",
            "call_type": "aresponses",
            "custom_llm_provider": "openai",
            "additional_args": {
                "complete_input_dict": {
                    "model": "gpt-5.6-luna",
                    "input": [{"type": "message", "role": "user"}],
                }
            },
        },
    )

    record_upstream_response({"id": "resp_stream", "output": []})
    attempts = end_upstream_capture(token)

    assert attempts[0]["response"] == {"id": "resp_stream", "output": []}
